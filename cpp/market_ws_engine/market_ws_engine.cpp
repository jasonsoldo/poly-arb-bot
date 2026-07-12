#include <boost/asio/connect.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/ssl.hpp>
#include <boost/beast/core.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/beast/websocket.hpp>
#include <boost/property_tree/json_parser.hpp>
#include <boost/property_tree/ptree.hpp>
#include <algorithm>
#include <chrono>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <string>
#include <vector>

namespace asio = boost::asio; namespace beast = boost::beast; namespace websocket = beast::websocket;
using tcp = asio::ip::tcp; using ssl_socket = asio::ssl::stream<tcp::socket>; using boost::property_tree::ptree;
struct Book { std::map<double, double> bids; std::map<double, double> asks; };
struct Market { std::string up, down; double size = 10, fee = .07, active = 0; };
double now() { return std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count(); }
double val(const ptree& p, const std::string& key) { return p.get<double>(key, 0); }
void levels(Book& b, const ptree& rows, bool bid, bool clear) {
    auto& side = bid ? b.bids : b.asks; if (clear) side.clear();
    for (const auto& item : rows) { const auto& p = item.second; double price = val(p, "price"), size = val(p, "size"); if (size <= 0) side.erase(price); else side[price] = size; }
}
void change(Book& b, const ptree& p) {
    auto& side = p.get<std::string>("side", "") == "BUY" ? b.bids : b.asks;
    double price = val(p, "price"), size = val(p, "size");
    if (size <= 0) side.erase(price); else side[price] = size;
}
std::pair<double,double> vwap(const Book& b, double size) { double left=size, filled=0, notional=0; for (const auto& x:b.asks) { double take=std::min(left,x.second); filled+=take; notional+=take*x.first; left-=take; if(left<=1e-9)break; } return {filled,filled?notional/filled:0}; }
void evaluate(const std::string& id, Market& m, const std::map<std::string,Book>& books) {
    auto u=books.find(m.up), d=books.find(m.down); if(u==books.end()||d==books.end())return; auto uv=vwap(u->second,m.size), dv=vwap(d->second,m.size); bool fok=uv.first>=m.size&&dv.first>=m.size;
    double uf=uv.first*m.fee*uv.second*(1-uv.second), df=dv.first*m.fee*dv.second*(1-dv.second), total=m.size*(uv.second+dv.second)+uf+df, profit=fok?m.size-total:0; bool good=fok&&profit>0; double t=now(); if(good&&m.active==0)m.active=t; if(!good)m.active=0;
    if(good) std::cout<<"shadow_opportunity\t"<<id<<"\t"<<std::setprecision(12)<<uv.second<<"\t"<<dv.second<<"\t"<<uf<<"\t"<<df<<"\t"<<total<<"\t"<<profit<<"\t1\t1\t"<<t-m.active<<"\n"<<std::flush;
}
bool btc_short_market(const std::string& slug) { return slug.find("btc-updown-5m-") != std::string::npos || slug.find("btc-updown-15m-") != std::string::npos; }
void subscribe_assets(websocket::stream<ssl_socket>& ws, const std::vector<std::string>& assets) {
    std::string message = "{\"assets_ids\":["; for (size_t i=0;i<assets.size();++i) { if (i) message += ","; message += "\"" + assets[i] + "\""; } message += "],\"operation\":\"subscribe\",\"custom_feature_enabled\":true}";
    ws.text(true); ws.write(asio::buffer(message)); std::cerr << "SUBSCRIBE_DYNAMIC " << message << "\n";
}
int main(int argc,char**argv){
    if(argc<2){std::cerr<<"usage: market_ws_engine <markets.json> [size] [fee_rate]\n";return 2;}
    std::ifstream file(argv[1]); ptree root; boost::property_tree::read_json(file,root); std::map<std::string,Market> markets; std::map<std::string,Book> books;
    for(const auto& item:root.get_child("markets")){const auto&p=item.second;std::string id=p.get<std::string>("market_id"),up=p.get<std::string>("up_token_id"),down=p.get<std::string>("down_token_id");markets[id]={up,down,argc>2?std::stod(argv[2]):10,argc>3?std::stod(argv[3]):.07,0};books[up];books[down];}
    if (books.empty()) { std::cerr << "NO_TOKENS live_markets.json contains no valid Up/Down tokens; rerun scan-updown\n"; return 4; }
    asio::io_context io; asio::ssl::context ctx(asio::ssl::context::tls_client); ctx.set_default_verify_paths(); ctx.set_verify_mode(asio::ssl::verify_peer); ssl_socket stream(io,ctx); stream.set_verify_callback(asio::ssl::host_name_verification("ws-subscriptions-clob.polymarket.com")); tcp::resolver resolver(io); auto endpoints=resolver.resolve("ws-subscriptions-clob.polymarket.com","443"); asio::connect(stream.next_layer(),endpoints); if(!SSL_set_tlsext_host_name(stream.native_handle(),"ws-subscriptions-clob.polymarket.com")) throw beast::system_error(beast::error_code(static_cast<int>(::ERR_get_error()),asio::error::get_ssl_category())); stream.handshake(asio::ssl::stream_base::client); websocket::stream<ssl_socket> ws(std::move(stream)); ws.handshake("ws-subscriptions-clob.polymarket.com","/ws/market");
    const size_t initial_count = std::min<size_t>(20, books.size());
    std::string subscribe="{\"assets_ids\":[";
    bool first=true; size_t index=0;
    for(const auto&x:books){if(index++>=initial_count) break; if(!first)subscribe+=",";first=false;subscribe+="\""+x.first+"\"";}
    subscribe+="] ,\"type\":\"market\",\"custom_feature_enabled\":true}";
    ws.text(true);
    ws.write(asio::buffer(subscribe));
    std::cerr<<"SUBSCRIBE "<<subscribe<<"\n";
    std::cout<<"connected\n";
    std::vector<std::string> extra; index=0;
    for(const auto&x:books) if(index++>=initial_count) extra.push_back(x.first);
    for(size_t offset=0; offset<extra.size(); offset+=20) {
        const size_t end=std::min(offset+20, extra.size());
        subscribe_assets(ws, std::vector<std::string>(extra.begin()+offset, extra.begin()+end));
    }
    for(;;){
        beast::flat_buffer buffer;
        try { ws.read(buffer); }
        catch (const beast::system_error& error) {
            std::cerr << "WS_READ_ERROR code=" << error.code().value() << " message=" << error.code().message() << "\n";
            return 3;
        }
        std::stringstream input;input<<beast::make_printable(buffer.data());std::cerr<<"WS_FRAME "<<input.str()<<"\n";ptree msg;
        try{std::istringstream json(input.str());boost::property_tree::read_json(json,msg);}catch(...){continue;}
        std::string type=msg.get<std::string>("event_type","");std::string asset=msg.get<std::string>("asset_id","");
        if(type=="new_market") {
            std::string slug=msg.get<std::string>("slug",""); auto assets=msg.get_child("assets_ids",ptree{}); std::vector<std::string> ids;
            for(const auto& item:assets) ids.push_back(item.second.get_value<std::string>());
            if(btc_short_market(slug)&&ids.size()>=2) { std::string id=msg.get<std::string>("market",""); markets[id]={ids[0],ids[1],argc>2?std::stod(argv[2]):10,argc>3?std::stod(argv[3]):.07,0}; books[ids[0]]; books[ids[1]]; subscribe_assets(ws,ids); }
        }
        else if(type=="book"&&books.count(asset)){levels(books[asset],msg.get_child("bids",ptree{}),true,true);levels(books[asset],msg.get_child("asks",ptree{}),false,true);}
        else if(type=="price_change"){for(const auto&x:msg.get_child("price_changes",ptree{})){const auto&p=x.second;std::string a=p.get<std::string>("asset_id",asset);if(books.count(a))change(books[a],p);}}
        for(auto&x:markets)evaluate(x.first,x.second,books);
    }
}
