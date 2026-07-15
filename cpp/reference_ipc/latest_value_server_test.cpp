#include "latest_value_server.hpp"

#include <boost/asio/read_until.hpp>

#include <cassert>
#include <chrono>
#include <filesystem>
#include <iostream>
#include <sstream>
#include <thread>

using Socket = boost::asio::local::stream_protocol::socket;

std::string unique_path() {
    const auto ticks = std::chrono::steady_clock::now().time_since_epoch().count();
    return (std::filesystem::temp_directory_path() /
            ("poly-reference-" + std::to_string(ticks) + ".sock")).string();
}

bool wait_until(const std::function<bool()>& predicate) {
    for (int i = 0; i < 200; ++i) {
        if (predicate()) return true;
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    return false;
}

void test_client_receives_latest_snapshot_on_connect() {
    boost::asio::io_context io;
    const auto path = unique_path();
    reference_ipc::LatestValueServer server(io, path);
    server.start();
    std::thread worker([&] { io.run(); });
    server.publish("latest\n");
    boost::asio::io_context client_io;
    Socket client(client_io);
    client.connect(boost::asio::local::stream_protocol::endpoint(path));
    boost::asio::streambuf buffer;
    boost::asio::read_until(client, buffer, '\n');
    std::istream input(&buffer);
    std::string line;
    std::getline(input, line);
    assert(line == "latest");
    client.close();
    io.stop();
    worker.join();
}

void test_slow_client_keeps_only_latest_pending_frame() {
    boost::asio::io_context io;
    const auto path = unique_path();
    reference_ipc::LatestValueServer server(io, path);
    server.start();
    std::thread worker([&] { io.run(); });
    boost::asio::io_context client_io;
    Socket client(client_io);
    client.connect(boost::asio::local::stream_protocol::endpoint(path));
    assert(wait_until([&] { return server.client_count() == 1; }));
    const std::string payload(1024 * 1024, 'x');
    for (int i = 0; i < 20; ++i) server.publish(payload + std::to_string(i) + "\n");
    assert(wait_until([&] { return server.coalesced_updates() > 0; }));
    client.close();
    io.stop();
    worker.join();
}

void test_disconnected_client_is_removed() {
    boost::asio::io_context io;
    const auto path = unique_path();
    reference_ipc::LatestValueServer server(io, path);
    server.start();
    std::thread worker([&] { io.run(); });
    {
        boost::asio::io_context client_io;
        Socket client(client_io);
        client.connect(boost::asio::local::stream_protocol::endpoint(path));
        assert(wait_until([&] { return server.client_count() == 1; }));
        client.close();
    }
    for (int i = 0; i < 10 && server.client_count(); ++i) {
        server.publish(std::string(256 * 1024, 'y') + "\n");
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    assert(wait_until([&] { return server.client_count() == 0; }));
    io.stop();
    worker.join();
}

void test_socket_path_is_cleaned_up() {
    const auto path = unique_path();
    boost::asio::io_context io;
    {
        reference_ipc::LatestValueServer server(io, path);
#ifndef _WIN32
        assert(std::filesystem::exists(path));
#endif
    }
    assert(!std::filesystem::exists(path));
}

int main() {
    test_client_receives_latest_snapshot_on_connect();
    test_slow_client_keeps_only_latest_pending_frame();
    test_disconnected_client_is_removed();
    test_socket_path_is_cleaned_up();
    std::cout << "latest-value server tests passed\n";
}
