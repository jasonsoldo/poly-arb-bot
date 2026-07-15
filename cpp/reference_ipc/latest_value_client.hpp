#pragma once

#include "reference_snapshot.hpp"

#include <boost/asio.hpp>
#include <boost/asio/local/stream_protocol.hpp>

#include <array>
#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>

namespace reference_ipc {

class LatestValueClient : public std::enable_shared_from_this<LatestValueClient> {
public:
    static constexpr std::size_t MAX_FRAME_BYTES = 1024 * 1024;
    static constexpr std::size_t READ_BUFFER_BYTES = 64 * 1024;
    static constexpr std::chrono::milliseconds COALESCE_WINDOW{1};
    using SnapshotHandler = std::function<void(const Snapshot&)>;
    using StateHandler = std::function<void(bool)>;

    LatestValueClient(
            boost::asio::io_context& io,
            std::string socket_path,
            SnapshotHandler snapshot_handler,
            StateHandler state_handler = {},
            std::chrono::milliseconds reconnect_delay = std::chrono::milliseconds(500))
        : io_(io), socket_path_(std::move(socket_path)), socket_(io),
          reconnect_timer_(io), delivery_timer_(io),
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
        delivery_timer_.cancel();
        socket_.cancel(ignored);
        socket_.close(ignored);
        set_connected(false);
    }

    bool connected() const { return connected_; }
    std::uint64_t sequence() const { return sequence_; }
    const std::string& producer_session() const { return producer_session_; }
    std::uint64_t protocol_errors() const { return protocol_errors_; }
    std::uint64_t reconnects() const { return reconnects_; }
    std::uint64_t coalesced_frames() const { return coalesced_frames_; }
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
            self->read_some();
        });
    }

    void read_some() {
        if (stopped_ || !connected_) return;
        auto self = shared_from_this();
        socket_.async_read_some(boost::asio::buffer(read_buffer_),
            [self](const boost::system::error_code& error, std::size_t bytes) {
                if (bytes) self->consume_bytes(bytes);
                if (error) {
                    self->flush_latest();
                    if (!self->connected_) return;
                    return self->disconnect_and_reconnect();
                }
                self->read_some();
            });
    }

    void consume_bytes(std::size_t bytes) {
        pending_input_.append(read_buffer_.data(), bytes);
        std::size_t newline = 0;
        while ((newline = pending_input_.find('\n')) != std::string::npos) {
            if (newline > MAX_FRAME_BYTES) {
                ++protocol_errors_;
                return disconnect_and_reconnect();
            }
            std::string line = pending_input_.substr(0, newline);
            pending_input_.erase(0, newline + 1);
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
                if (latest_snapshot_) ++coalesced_frames_;
                latest_snapshot_ = std::move(snapshot);
            } catch (const std::exception&) {
                ++protocol_errors_;
                return disconnect_and_reconnect();
            }
        }
        if (pending_input_.size() > MAX_FRAME_BYTES) {
            ++protocol_errors_;
            return disconnect_and_reconnect();
        }
        schedule_delivery();
    }

    void schedule_delivery() {
        if (!latest_snapshot_ || delivery_scheduled_ || stopped_ || !connected_) return;
        delivery_scheduled_ = true;
        delivery_timer_.expires_after(COALESCE_WINDOW);
        auto self = shared_from_this();
        delivery_timer_.async_wait([self](const boost::system::error_code& error) {
            if (error || self->stopped_ || !self->connected_) return;
            self->delivery_scheduled_ = false;
            self->deliver_latest();
        });
    }

    void flush_latest() {
        delivery_timer_.cancel();
        delivery_scheduled_ = false;
        deliver_latest();
    }

    void deliver_latest() {
        if (!latest_snapshot_) return;
        Snapshot snapshot = std::move(*latest_snapshot_);
        latest_snapshot_.reset();
        last_received_at_ = std::chrono::steady_clock::now();
        if (snapshot_handler_) snapshot_handler_(snapshot);
    }

    void disconnect_and_reconnect() {
        boost::system::error_code ignored;
        socket_.cancel(ignored);
        socket_.close(ignored);
        delivery_timer_.cancel();
        delivery_scheduled_ = false;
        pending_input_.clear();
        latest_snapshot_.reset();
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
    boost::asio::steady_timer delivery_timer_;
    std::array<char, READ_BUFFER_BYTES> read_buffer_{};
    std::string pending_input_;
    std::optional<Snapshot> latest_snapshot_;
    SnapshotHandler snapshot_handler_;
    StateHandler state_handler_;
    std::chrono::milliseconds reconnect_delay_;
    bool stopped_ = true;
    bool connected_ = false;
    bool delivery_scheduled_ = false;
    std::string producer_session_;
    std::uint64_t sequence_ = 0;
    std::uint64_t protocol_errors_ = 0;
    std::uint64_t reconnects_ = 0;
    std::uint64_t coalesced_frames_ = 0;
    std::chrono::steady_clock::time_point last_received_at_{};
};

}  // namespace reference_ipc
