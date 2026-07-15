#include "latest_value_client.hpp"

#include <boost/asio.hpp>
#include <boost/asio/local/stream_protocol.hpp>

#include <cassert>
#include <chrono>
#include <filesystem>
#include <functional>
#include <iostream>
#include <memory>
#include <string>
#include <vector>

namespace asio = boost::asio;
using Socket = asio::local::stream_protocol::socket;

std::string unique_path() {
    const auto ticks = std::chrono::steady_clock::now().time_since_epoch().count();
    return (std::filesystem::temp_directory_path() /
            ("poly-reference-client-" + std::to_string(ticks) + ".sock")).string();
}

reference_ipc::Snapshot snapshot(const std::string& session, std::uint64_t sequence) {
    reference_ipc::Snapshot value;
    value.producer_session = session;
    value.sequence = sequence;
    value.produced_monotonic_ns = sequence;
    value.produced_wall_ms = 1'700'000'000'000.0 + sequence;
    return value;
}

class ScriptServer {
public:
    ScriptServer(asio::io_context& io, std::string path,
                 std::vector<std::vector<std::string>> connections)
        : io_(io), path_(std::move(path)), acceptor_(io), connections_(std::move(connections)) {
        std::error_code ignored;
        std::filesystem::remove(path_, ignored);
        asio::local::stream_protocol::endpoint endpoint(path_);
        acceptor_.open(endpoint.protocol());
        acceptor_.bind(endpoint);
        acceptor_.listen();
        accept_next();
    }

    ~ScriptServer() {
        boost::system::error_code ignored;
        acceptor_.close(ignored);
        std::error_code filesystem_error;
        std::filesystem::remove(path_, filesystem_error);
    }

    std::size_t accepts() const { return accepts_; }

private:
    struct Connection : std::enable_shared_from_this<Connection> {
        Connection(asio::io_context& io, Socket socket, std::vector<std::string> chunks)
            : socket(std::move(socket)), timer(io), chunks(std::move(chunks)) {}

        void start() { write_next(); }

        void write_next() {
            if (index == chunks.size()) {
                boost::system::error_code ignored;
                socket.shutdown(Socket::shutdown_both, ignored);
                socket.close(ignored);
                return;
            }
            active = chunks[index++];
            auto self = shared_from_this();
            asio::async_write(socket, asio::buffer(active), [self](auto error, std::size_t) {
                if (error) return;
                self->timer.expires_after(std::chrono::milliseconds(5));
                self->timer.async_wait([self](auto timer_error) {
                    if (!timer_error) self->write_next();
                });
            });
        }

        Socket socket;
        asio::steady_timer timer;
        std::vector<std::string> chunks;
        std::size_t index = 0;
        std::string active;
    };

    void accept_next() {
        if (accepts_ >= connections_.size()) return;
        acceptor_.async_accept([this](auto error, Socket socket) {
            if (error) return;
            auto connection = std::make_shared<Connection>(
                io_, std::move(socket), connections_[accepts_++]);
            connection->start();
            accept_next();
        });
    }

    asio::io_context& io_;
    std::string path_;
    asio::local::stream_protocol::acceptor acceptor_;
    std::vector<std::vector<std::string>> connections_;
    std::size_t accepts_ = 0;
};

template <typename Predicate>
void run_until(asio::io_context& io, Predicate predicate, int timeout_ms = 1000) {
    asio::steady_timer deadline(io);
    bool timed_out = false;
    deadline.expires_after(std::chrono::milliseconds(timeout_ms));
    deadline.async_wait([&](auto error) { if (!error) timed_out = true; });
    while (!predicate() && !timed_out) {
        if (io.run_one() == 0) io.restart();
    }
    deadline.cancel();
    assert(predicate());
}

void test_fragmented_frame() {
    asio::io_context io;
    const auto path = unique_path();
    const auto line = reference_ipc::encode_line(snapshot("a", 1));
    ScriptServer server(io, path, {{line.substr(0, line.size() / 2), line.substr(line.size() / 2)}});
    std::vector<reference_ipc::Snapshot> received;
    auto client = std::make_shared<reference_ipc::LatestValueClient>(
        io, path, [&](const auto& value) { received.push_back(value); });
    client->start();
    run_until(io, [&] { return received.size() == 1; });
    assert(received[0].sequence == 1);
    client->stop();
}

void test_combined_frames() {
    asio::io_context io;
    const auto path = unique_path();
    ScriptServer server(io, path, {{reference_ipc::encode_line(snapshot("a", 1)) +
                                    reference_ipc::encode_line(snapshot("a", 2))}});
    std::vector<std::uint64_t> received;
    auto client = std::make_shared<reference_ipc::LatestValueClient>(
        io, path, [&](const auto& value) { received.push_back(value.sequence); });
    client->start();
    run_until(io, [&] { return received.size() == 2; });
    assert((received == std::vector<std::uint64_t>{1, 2}));
    client->stop();
}

void test_malformed_frame_is_discarded() {
    asio::io_context io;
    const auto path = unique_path();
    ScriptServer server(io, path, {{"not-json\n" + reference_ipc::encode_line(snapshot("a", 1))}});
    std::size_t received = 0;
    auto client = std::make_shared<reference_ipc::LatestValueClient>(
        io, path, [&](const auto&) { ++received; });
    client->start();
    run_until(io, [&] { return received == 1; });
    assert(client->protocol_errors() == 1);
    client->stop();
}

void test_sequence_rollback_invalidates_connection() {
    asio::io_context io;
    const auto path = unique_path();
    ScriptServer server(io, path, {{reference_ipc::encode_line(snapshot("a", 2)) +
                                    reference_ipc::encode_line(snapshot("a", 1))}});
    std::vector<bool> states;
    auto client = std::make_shared<reference_ipc::LatestValueClient>(
        io, path, [](const auto&) {}, [&](bool connected) { states.push_back(connected); },
        std::chrono::milliseconds(10));
    client->start();
    run_until(io, [&] { return client->protocol_errors() == 1; });
    assert(!client->connected());
    assert(!states.empty() && states.back() == false);
    client->stop();
}

void test_new_producer_session_is_accepted() {
    asio::io_context io;
    const auto path = unique_path();
    ScriptServer server(io, path, {{reference_ipc::encode_line(snapshot("a", 9)) +
                                    reference_ipc::encode_line(snapshot("b", 1))}});
    std::vector<std::string> sessions;
    auto client = std::make_shared<reference_ipc::LatestValueClient>(
        io, path, [&](const auto& value) { sessions.push_back(value.producer_session); });
    client->start();
    run_until(io, [&] { return sessions.size() == 2; });
    assert((sessions == std::vector<std::string>{"a", "b"}));
    assert(client->sequence() == 1);
    client->stop();
}

void test_eof_reconnects() {
    asio::io_context io;
    const auto path = unique_path();
    ScriptServer server(io, path, {
        {reference_ipc::encode_line(snapshot("a", 1))},
        {reference_ipc::encode_line(snapshot("b", 1))},
    });
    std::vector<std::string> sessions;
    auto client = std::make_shared<reference_ipc::LatestValueClient>(
        io, path, [&](const auto& value) { sessions.push_back(value.producer_session); },
        [](bool) {}, std::chrono::milliseconds(10));
    client->start();
    run_until(io, [&] { return sessions.size() == 2; });
    assert(server.accepts() == 2);
    assert(client->reconnects() >= 1);
    client->stop();
}

int main() {
    test_fragmented_frame();
    test_combined_frames();
    test_malformed_frame_is_discarded();
    test_sequence_rollback_invalidates_connection();
    test_new_producer_session_is_accepted();
    test_eof_reconnects();
    std::cout << "latest-value client tests passed\n";
}
