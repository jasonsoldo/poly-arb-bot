#pragma once

#include <boost/asio.hpp>
#include <boost/asio/local/stream_protocol.hpp>

#include <algorithm>
#include <atomic>
#include <filesystem>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace reference_ipc {

class LatestValueServer {
    using LocalSocket = boost::asio::local::stream_protocol::socket;

    class Client : public std::enable_shared_from_this<Client> {
    public:
        Client(LocalSocket socket, LatestValueServer& owner)
            : socket_(std::move(socket)), owner_(owner) {}

        void send(std::string frame) {
            if (writing_) {
                if (pending_frame_) ++owner_.coalesced_updates_;
                pending_frame_ = std::move(frame);
                return;
            }
            writing_ = true;
            active_frame_ = std::move(frame);
            write_active();
        }

    private:
        void write_active() {
            auto self = shared_from_this();
            boost::asio::async_write(socket_, boost::asio::buffer(active_frame_),
                [self](const boost::system::error_code& error, std::size_t) {
                    if (error) return self->owner_.remove(self);
                    if (self->pending_frame_) {
                        self->active_frame_ = std::move(*self->pending_frame_);
                        self->pending_frame_.reset();
                        self->write_active();
                    } else {
                        self->writing_ = false;
                        self->active_frame_.clear();
                    }
                });
        }

        LocalSocket socket_;
        LatestValueServer& owner_;
        bool writing_ = false;
        std::string active_frame_;
        std::optional<std::string> pending_frame_;
    };

public:
    LatestValueServer(boost::asio::io_context& io, std::string socket_path)
        : io_(io), socket_path_(std::move(socket_path)), acceptor_(io) {
        const std::filesystem::path path(socket_path_);
        if (!path.parent_path().empty()) std::filesystem::create_directories(path.parent_path());
        std::error_code ignored;
        std::filesystem::remove(socket_path_, ignored);
        const boost::asio::local::stream_protocol::endpoint endpoint(socket_path_);
        acceptor_.open(endpoint.protocol());
        acceptor_.bind(endpoint);
        acceptor_.listen();
    }

    ~LatestValueServer() {
        boost::system::error_code ignored;
        acceptor_.close(ignored);
        clients_.clear();
        std::error_code filesystem_error;
        std::filesystem::remove(socket_path_, filesystem_error);
    }

    void start() { async_accept(); }

    void publish(std::string frame) {
        boost::asio::post(io_, [this, frame = std::move(frame)]() mutable {
            latest_frame_ = std::move(frame);
            for (const auto& client : clients_) client->send(latest_frame_);
        });
    }

    std::size_t client_count() const { return client_count_.load(); }
    std::uint64_t coalesced_updates() const { return coalesced_updates_.load(); }
    const std::string& socket_path() const { return socket_path_; }

private:
    void async_accept() {
        acceptor_.async_accept([this](const boost::system::error_code& error, LocalSocket socket) {
            if (!error) {
                auto client = std::make_shared<Client>(std::move(socket), *this);
                clients_.push_back(client);
                client_count_.store(clients_.size());
                if (!latest_frame_.empty()) client->send(latest_frame_);
            }
            if (acceptor_.is_open()) async_accept();
        });
    }

    void remove(const std::shared_ptr<Client>& client) {
        clients_.erase(std::remove(clients_.begin(), clients_.end(), client), clients_.end());
        client_count_.store(clients_.size());
    }

    boost::asio::io_context& io_;
    std::string socket_path_;
    boost::asio::local::stream_protocol::acceptor acceptor_;
    std::vector<std::shared_ptr<Client>> clients_;
    std::string latest_frame_;
    std::atomic<std::size_t> client_count_{0};
    std::atomic<std::uint64_t> coalesced_updates_{0};
};

}  // namespace reference_ipc
