# server.py
import argparse
import asyncio
import json
import logging
import os
import time
import requests
import base64
from typing import Set, Dict, Any

from aiohttp import web
from aiohttp_cors import setup as cors_setup, ResourceOptions
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack, RTCConfiguration, RTCIceServer
from aiortc.contrib.media import MediaBlackhole

# Twilio SDK 추가
from twilio.rest import Client

# Twilio 및 Railway 환경 관련 코드 - Twilio ICE 서버 사용으로 변경
_ice_servers_cache = None
_cache_timestamp = 0
_cache_ttl = 3600  # 1시간

def get_ice_servers():
    """Twilio ICE 서버 또는 환경변수 기반 coturn/무료 STUN 서버 제공"""
    global _ice_servers_cache, _cache_timestamp
    current_time = time.time()
    if _ice_servers_cache and (current_time - _cache_timestamp) < _cache_ttl:
        return _ice_servers_cache

    # Twilio ICE 서버 시도
    twilio_account_sid = os.environ.get('TWILIO_ACCOUNT_SID')
    twilio_auth_token = os.environ.get('TWILIO_AUTH_TOKEN')
    
    if twilio_account_sid and twilio_auth_token:
        try:
            client = Client(twilio_account_sid, twilio_auth_token)
            token = client.tokens.create()
            
            ice_servers = []
            for ice_server in token.ice_servers:
                ice_servers.append({
                    "urls": ice_server["urls"],
                    "username": ice_server.get("username"),
                    "credential": ice_server.get("credential")
                })
            
            logging.info(f"🔄 Twilio ICE 서버 사용: {len(ice_servers)}개 서버")
            _ice_servers_cache = ice_servers
            _cache_timestamp = current_time
            return ice_servers
            
        except Exception as e:
            logging.warning(f"⚠️ Twilio ICE 서버 가져오기 실패: {e}")
    
    # Twilio 실패 시 기존 로직 사용
    # 환경변수 기반 coturn 정보
    custom_turn = os.environ.get('CUSTOM_TURN_SERVER')
    custom_turn_user = os.environ.get('CUSTOM_TURN_USER', 'webrtc')
    custom_turn_pass = os.environ.get('CUSTOM_TURN_PASS', 'webrtc123')

    default_servers = [
        {"urls": "stun:stun.l.google.com:19302"},
        {"urls": "stun:stun1.l.google.com:19302"},
    ]

    # coturn 서버가 환경변수로 지정된 경우 추가
    if custom_turn:
        # Railway 등 PaaS에서는 반드시 public IP로 지정해야 외부에서 접근 가능
        # 예시: export CUSTOM_TURN_SERVER=xxx.xxx.xxx.xxx
        default_servers.append({
            "urls": f"turn:{custom_turn}:3478",
            "username": custom_turn_user,
            "credential": custom_turn_pass
        })
        logging.info(f"🔄 환경변수 기반 coturn TURN 서버 사용: {custom_turn}")
    else:
        logging.info("🔄 무료 STUN 서버만 사용 (TURN 미설정)")

    _ice_servers_cache = default_servers
    _cache_timestamp = current_time
    return default_servers

def convert_to_rtc_ice_servers(ice_servers_data):
    """클라이언트용 ICE 서버 데이터를 aiortc RTCIceServer 객체로 변환"""
    rtc_ice_servers = []
    
    for server in ice_servers_data:
        urls = server.get("urls", [])
        username = server.get("username")
        credential = server.get("credential")
        
        if username and credential:
            # TURN 서버 (TCP 우선 사용)
            if isinstance(urls, list):
                tcp_urls = [url for url in urls if 'transport=tcp' in url or ':443' in url]
                if not tcp_urls:
                    tcp_urls = urls  # TCP 전용이 없으면 모든 URL 사용
            else:
                tcp_urls = [urls]
            
            rtc_ice_servers.append(RTCIceServer(
                urls=tcp_urls,
                username=username,
                credential=credential
            ))
        else:
            # STUN 서버
            rtc_ice_servers.append(RTCIceServer(urls=urls))
    
    return rtc_ice_servers

ROOT = os.path.dirname(__file__)

# 전역 변수를 더 명확하게 타입 힌트와 함께
pcs: Set[RTCPeerConnection] = set()

# 서버 통계 정보
server_stats = {
    "start_time": time.time(),
    "total_connections": 0,
    "current_connections": 0,
    "failed_connections": 0,
    "connection_times": []
}

class AudioEchoTrack(MediaStreamTrack):
    """음성을 그대로 다시 보내는 Echo Track"""
    kind = "audio"

    def __init__(self, track: MediaStreamTrack):
        super().__init__()
        self.track = track

    async def recv(self):
        """받은 음성 프레임을 그대로 다시 전송"""
        try:
            frame = await self.track.recv()
            return frame
        except Exception as e:
            logging.error(f"AudioEchoTrack recv error: {e}")
            raise

async def index(request):
    """메인 페이지 제공"""
    return web.FileResponse(os.path.join(ROOT, "static/index.html"))

async def offer(request):
    """WebRTC offer 처리 및 answer 생성"""
    start_time = asyncio.get_event_loop().time()
    
    try:
        params = await request.json()
        offer_received_time = asyncio.get_event_loop().time()
        
        offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

        # 통일된 ICE 서버 설정 사용
        ice_servers_data = get_ice_servers()
        rtc_ice_servers = convert_to_rtc_ice_servers(ice_servers_data)
        
        pc = RTCPeerConnection(configuration=RTCConfiguration(
            iceServers=rtc_ice_servers,
            iceTransportPolicy="relay"  # TURN만 사용 (PaaS 환경 최적화)
        ))
        pcs.add(pc)
        pc_created_time = asyncio.get_event_loop().time()
        
        # 통계 업데이트
        server_stats["total_connections"] += 1
        
        logging.info(f"Created PeerConnection with {len(rtc_ice_servers)} ICE servers. Total connections: {len(pcs)}")

        @pc.on("track")
        def on_track(track):
            track_received_time = asyncio.get_event_loop().time()
            if track.kind == "audio":
                logging.info(f"Received audio track (after {(track_received_time - start_time)*1000:.1f}ms)")
                pc.addTrack(AudioEchoTrack(track))

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            connection_time = asyncio.get_event_loop().time()
            logging.info(f"Connection state changed: {pc.connectionState} (after {(connection_time - start_time)*1000:.1f}ms)")
            if pc.connectionState in ["failed", "closed"]:
                if pc in pcs:
                    pcs.discard(pc)
                    logging.info(f"Removed PeerConnection. Total connections: {len(pcs)}")
            elif pc.connectionState == "connected":
                total_time = (connection_time - start_time) * 1000
                server_stats["connection_times"].append(total_time)
                # 최근 100개 연결 시간만 유지
                if len(server_stats["connection_times"]) > 100:
                    server_stats["connection_times"] = server_stats["connection_times"][-100:]
                logging.info(f"🎉 WebRTC connection established in {total_time:.1f}ms")

        await pc.setRemoteDescription(offer)
        remote_desc_time = asyncio.get_event_loop().time()
        
        answer = await pc.createAnswer()
        answer_created_time = asyncio.get_event_loop().time()
        
        await pc.setLocalDescription(answer)
        local_desc_time = asyncio.get_event_loop().time()

        # 타이밍 정보 로깅
        timings = {
            "offer_processing": (offer_received_time - start_time) * 1000,
            "pc_creation": (pc_created_time - offer_received_time) * 1000,
            "remote_description": (remote_desc_time - pc_created_time) * 1000,
            "answer_creation": (answer_created_time - remote_desc_time) * 1000,
            "local_description": (local_desc_time - answer_created_time) * 1000,
            "total_server_time": (local_desc_time - start_time) * 1000
        }
        
        logging.info(f"Signaling timings: {timings}")

        return web.Response(
            content_type="application/json",
            text=json.dumps({
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type,
                "server_timings": timings  # 클라이언트에서 참고할 수 있도록
            })
        )
    
    except Exception as e:
        error_time = asyncio.get_event_loop().time()
        server_stats["failed_connections"] += 1
        logging.error(f"Error in offer handling after {(error_time - start_time)*1000:.1f}ms: {e}")
        return web.Response(
            status=500,
            content_type="application/json",
            text=json.dumps({"error": str(e)})
        )

async def on_shutdown(app):
    """앱 종료 시 모든 연결 정리"""
    logging.info(f"Shutting down. Closing {len(pcs)} connections.")
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros, return_exceptions=True)
    pcs.clear()

async def get_stats(request):
    """서버 통계 정보 반환"""
    current_time = time.time()
    uptime = current_time - server_stats["start_time"]
    
    # 최근 연결 시간 평균 계산 (최근 10개)
    recent_times = server_stats["connection_times"][-10:]
    avg_connection_time = sum(recent_times) / len(recent_times) if recent_times else 0
    
    stats = {
        "server_status": "running",
        "uptime_seconds": uptime,
        "current_connections": len(pcs),
        "total_connections": server_stats["total_connections"],
        "failed_connections": server_stats["failed_connections"],
        "success_rate": ((server_stats["total_connections"] - server_stats["failed_connections"]) / 
                        max(server_stats["total_connections"], 1)) * 100,
        "average_connection_time_ms": avg_connection_time,
        "memory_usage": {
            "active_peer_connections": len(pcs)
        },
        "performance": {
            "connections_per_minute": (server_stats["total_connections"] / (uptime / 60)) if uptime > 60 else 0
        },
        "ice_servers_info": {
            "cache_age_seconds": time.time() - _cache_timestamp if _cache_timestamp else 0,
            "total_ice_servers": len(_ice_servers_cache) if _ice_servers_cache else 0
        }
    }
    
    return web.Response(
        content_type="application/json",
        text=json.dumps(stats, indent=2)
    )

async def health_check(request):
    """헬스 체크 엔드포인트 (Railway용)"""
    return web.Response(
        content_type="application/json",
        text=json.dumps({
            "status": "healthy",
            "connections": len(pcs),
            "timestamp": time.time(),
            "platform": "Railway"
        })
    )

async def get_ice_servers_endpoint(request):
    """클라이언트용 ICE 서버 정보 제공 (서버와 동일한 설정)"""
    try:
        ice_servers = get_ice_servers()
        
        return web.Response(
            content_type="application/json",
            text=json.dumps({
                "iceServers": ice_servers,
                "cacheInfo": {
                    "age_seconds": time.time() - _cache_timestamp if _cache_timestamp else 0,
                    "ttl_seconds": _cache_ttl
                }
            })
        )
        
    except Exception as e:
        logging.error(f"ICE 서버 정보 제공 오류: {e}")
        # 에러 시 기본 STUN만 제공
        return web.Response(
            content_type="application/json",
            text=json.dumps({
                "iceServers": [
                    {"urls": "stun:stun.l.google.com:19302"},
                    {"urls": "stun:stun1.l.google.com:19302"},
                ],
                "error": "Failed to get Twilio servers, using fallback"
            })
        )

async def refresh_ice_servers(request):
    """ICE 서버 캐시 강제 새로고침"""
    global _ice_servers_cache, _cache_timestamp
    
    try:
        # 캐시 초기화
        _ice_servers_cache = None
        _cache_timestamp = 0
        
        # 새로 가져오기
        ice_servers = get_ice_servers()
        
        return web.Response(
            content_type="application/json",
            text=json.dumps({
                "status": "refreshed",
                "iceServers": ice_servers,
                "message": "ICE servers cache refreshed successfully"
            })
        )
        
    except Exception as e:
        logging.error(f"ICE 서버 캐시 새로고침 오류: {e}")
        return web.Response(
            status=500,
            content_type="application/json",
            text=json.dumps({"error": str(e)})
        )

def create_app():
    """웹 애플리케이션 생성 및 설정"""
    # 서버 시작 시 ICE 서버 캐시 초기화
    global _ice_servers_cache, _cache_timestamp
    _ice_servers_cache = None
    _cache_timestamp = 0
    
    app = web.Application()
    
    # CORS 설정
    cors = cors_setup(app, defaults={
        "*": ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
            allow_methods="*"
        )
    })
    
    # 라우트 등록
    app.router.add_get("/", index)
    cors.add(app.router.add_post("/offer", offer))
    cors.add(app.router.add_get("/stats", get_stats))
    cors.add(app.router.add_get("/health", health_check))
    cors.add(app.router.add_get("/ice-servers", get_ice_servers_endpoint))
    cors.add(app.router.add_post("/refresh-ice", refresh_ice_servers))  # 캐시 새로고침 엔드포인트
    
    # 정적 파일 서비스
    app.router.add_static("/static", os.path.join(ROOT, "static"))
    
    # 종료 핸들러 등록
    app.on_shutdown.append(on_shutdown)
    
    return app

if __name__ == "__main__":
    # Railway 환경변수에서 포트 읽기
    port = int(os.environ.get("PORT", 8080))
    
    # 로깅 설정
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    # 서버 시작 시 환경변수 확인을 위한 디버그 로그
    print("DEBUG: CUSTOM_TURN_SERVER =", os.environ.get("CUSTOM_TURN_SERVER"))
    print("DEBUG: CUSTOM_TURN_USER =", os.environ.get("CUSTOM_TURN_USER"))
    print("DEBUG: CUSTOM_TURN_PASS =", os.environ.get("CUSTOM_TURN_PASS"))
    print("DEBUG: TWILIO_ACCOUNT_SID =", os.environ.get("TWILIO_ACCOUNT_SID"))
    print("DEBUG: TWILIO_AUTH_TOKEN =", "***" if os.environ.get("TWILIO_AUTH_TOKEN") else None)

    logging.info(f"🚀 Starting WebRTC Echo Server on Railway (port {port})")
    logging.info(f"🔧 Twilio ICE: {'✅ Configured' if os.environ.get('TWILIO_ACCOUNT_SID') else '❌ Not configured'}")
    logging.info(f"🔧 Custom TURN: {'✅ Configured' if os.environ.get('CUSTOM_TURN_SERVER') else '❌ Not configured'}")
    
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=port)
