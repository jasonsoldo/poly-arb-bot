#pragma once

#include "reference_snapshot.hpp"

#include <boost/asio.hpp>
#include <boost/asio/local/stream_protocol.hpp>

#include <chrono>
#include <cstdint>
#include <functional>
#include <istream>
#include <memory>
#include <string>

namespace reference_ipc {

class LatestValueClient : public std::enable_shared_from_this<LatestValueClient> {
public:
    static constexpr std::size_t MAX_FRAME_BYTES = 1024 * 1024;
    using SnapshotHandler = std::function<void(const Snapshot&)>;
    using StateHandler = std::function<void(bool)>;

    LatestValueClient(
            boost::asio::io_context& io,
            std::string socket_path,
            SnapshotHandler snapshot_handler,
            StateHandler state_handler = {},
            std::chrono::milliseconds reconnect_delay = std::chrono::milliseconds(500))
        : io_(io), socket_path_(std::move(socket_path)), socket_(io),
          reconnect_timer_(io), input_(MAX_FRAME_BYTES),
          snapshot_handler_(std::move(snapshot_handler)),
          state_handler_(std::move(state_handler)), reconnect_delay_(reconnect_delay) {}

    void start() {
        stopped_ = false;
        connect();
    }

    void stop() {
        stopped_ = true;
        boost::system::error_code ignored;
        reconnect_timer_.cancel();
        socket_.cancel(ignored);
        socket_.close(ignored);
        set_connected(false);
    }

    bool connected() const { return connected_; }
    std::uint64_t sequence() const { return sequence_; }
    const std::string& producer_session() const { return producer_session_; }
    std::uint64_t protocol_errors() const { return protocol_errors_; }
    std::uint64_t reconnects() const { return reconnects_; }
    std::chrono::steady_clock::time_point last_received_at() const { return last_received_at_; }

private:
    void connect() {
        if (stopped_) return;
        boost::system::error_code ignored;
        socket_.close(ignored);
        socket_ = boost::asio::local::stream_protocol::socket(io_);
        const boost::asio::local::stream_protocol::endpoint endpoint(socket_path_);
        auto self = shared_from_this();
        socket_.async_connect(endpoint, [self](const boost::system::error_code& error) {
            if (error) return self->schedule_reconnect();
            self->set_connected(true);
            self->read_frame();
        });
    }

    void read_frame() {
        if (stopped_ || !connected_) return;
        auto self = shared_from_this();
        boost::asio::async_read_until(socket_, input_, '\n',
            [self](const boost::system::error_code& error, std::size_t bytes) {
                if (error) {
                    if (self->input_.size() >= MAX_FRAME_BYTES) ++self->protocol_errors_;
                    return self->disconnect_and_reconnect();
                }
                self->handle_frame(bytes);
            });
    }

    void handle_frame(std::size_t) {
        std::istream input(&input_);
        std::string line;
        std::getline(input, line);
        try {
            Snapshot snapshot = decode_line(line);
            if (snapshot.producer_session == producer_session_ &&
                producer_session_.size() && snapshot.sequence <= sequence_) {
                // A same-session sequence rollback means the stream cannot be trusted.
                ++protocol_errors_;
                return disconnect_and_reconnect();
            }
            producer_session_ = snapshot.producer_session;
            sequence_ = snapshot.sequence;
            last_received_at_ = std::chrono::steady_clock::now();
            if (snapshot_handler_) snapshot_handler_(snapshot);
        } catch (const std::exception&) {
            ++protocol_errors_;
        }
        read_frame();
    }

    void disconnect_and_reconnect() {
        boost::system::error_code ignored;
        socket_.cancel(ignored);
        socket_.close(ignored);
        input_.consume(input_.size());
        set_connected(false);
        schedule_reconnect();
    }

    void schedule_reconnect() {
        if (stopped_) return;
        set_connected(false);
        ++reconnects_;
        reconnect_timer_.expires_after(reconnect_delay_);
        auto self = shared_from_this();
        reconnect_timer_.async_wait([self](const boost::system::error_code& error) {
            if (!error && !self->stopped_) self->connect();
        });
    }

    void set_connected(bool connected) {
        if (connected_ == connected) return;
        connected_ = connected;
        if (state_handler_) state_handler_(connected_);
    }

    boost::asio::io_context& io_;
    std::string socket_path_;
    boost::asio::local::stream_protocol::socket socket_;
    boost::asio::steady_timer reconnect_timer_;
    boost::asio::streambuf input_;
    SnapshotHandler snapshot_handler_;
    StateHandler state_handler_;
    std::chrono::milliseconds reconnect_delay_;
    bool stopped_ = true;
    bool connected_ = false;
    std::string producer_session_;
    std::uint64_t sequence_ = 0;
    std::uint64_t protocol_errors_ = 0;
    std::uint64_t reconnects_ = 0;
    std::chrono::steady_clock::time_point last_received_at_{};
};

}  // namespace reference_ipc
