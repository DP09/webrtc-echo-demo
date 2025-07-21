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

# WebRTC 미디어 포트 범위 설정 (Fly.io용)
import socket

# aiortc ICE 포트 범위 설정
os.environ['AIORTC_ICE_PORT_MIN'] = '8000'
os.environ['AIORTC_ICE_PORT_MAX'] = '8004'

def get_twilio_ice_servers():
    """Twilio API에서 ICE 서버 정보를 동적으로 가져옵니다."""
    
    account_sid = os.environ.get('TWILIO_ACCOUNT_SID')
    auth_token = os.environ.get('TWILIO_AUTH_TOKEN')
    
    if not account_sid or not auth_token:
        logging.warning("Twilio 계정 정보가 없어서 기본 STUN만 사용합니다.")
        return [
            RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
            RTCIceServer(urls=["stun:stun1.l.google.com:19302"]),
        ]
    
    try:
        # Twilio API 호출
        url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Tokens.json"
        credentials = f"{account_sid}:{auth_token}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()
        
        headers = {
            "Authorization": f"Basic {encoded_credentials}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        response = requests.post(url, headers=headers, data={"Ttl": 3600})
        response.raise_for_status()
        
        token_data = response.json()
        ice_servers_data = token_data.get("ice_servers", [])
        
        # aiortc RTCIceServer 객체로 변환 (TCP 전용)
        ice_servers = []
        for server in ice_servers_data:
            urls = server.get("urls", [])
            username = server.get("username")
            credential = server.get("credential")
            
            # TCP TURN만 사용 (UDP 포트 제약 회피)
            if isinstance(urls, list):
                tcp_urls = [url for url in urls if 'transport=tcp' in url or ':443' in url]
            else:
                tcp_urls = [urls] if ('transport=tcp' in urls or ':443' in urls) else []
            
            if tcp_urls and username and credential:
                ice_servers.append(RTCIceServer(
                    urls=tcp_urls,
                    username=username,
                    credential=credential
                ))
            elif not username:  # STUN 서버
                ice_servers.append(RTCIceServer(urls=urls))
        
        logging.info(f"✅ Twilio TURN 서버 {len(ice_servers)}개 로드 성공")
        return ice_servers
        
    except Exception as e:
        logging.error(f"❌ Twilio API 오류: {e}")
        # 실패 시 여러 TURN 서버 사용
        return [
            RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
            RTCIceServer(
                urls=["turn:openrelay.metered.ca:443?transport=tcp"],
                username="openrelayproject",
                credential="openrelayproject"
            ),
            RTCIceServer(
                urls=["turn:relay.metered.ca:443?transport=tcp"],
                username="bcc092b1b7f04dffbd7e",
                credential="iWN9kEtxDXF6VYEJ"
            ),
            RTCIceServer(
                urls=["turn:numb.viagenie.ca:443?transport=tcp"],
                username="webrtc@live.com",
                credential="muazkh"
            )
        ]

def setup_webrtc_ports():
    """WebRTC용 UDP 포트 확인"""
    logging.info(f"WebRTC ICE port range: {os.environ.get('AIORTC_ICE_PORT_MIN')}-{os.environ.get('AIORTC_ICE_PORT_MAX')}")
    
    # 포트 가용성 확인
    for port in range(8000, 8005):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(('0.0.0.0', port))
            sock.close()  # 즉시 닫기
            logging.info(f"UDP port {port} is available")
        except Exception as e:
            logging.warning(f"UDP port {port} not available: {e}")

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

        pc = RTCPeerConnection(configuration=RTCConfiguration(
            iceServers=get_twilio_ice_servers()
        ))
        pcs.add(pc)
        pc_created_time = asyncio.get_event_loop().time()
        
        # 통계 업데이트
        server_stats["total_connections"] += 1
        
        logging.info(f"Created PeerConnection. Total connections: {len(pcs)}")

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
        }
    }
    
    return web.Response(
        content_type="application/json",
        text=json.dumps(stats, indent=2)
    )

async def health_check(request):
    """헬스 체크 엔드포인트"""
    return web.Response(
        content_type="application/json",
        text=json.dumps({
            "status": "healthy",
            "connections": len(pcs),
            "timestamp": time.time()
        })
    )

async def get_ice_servers_endpoint(request):
    """클라이언트용 ICE 서버 정보 제공"""
    try:
        account_sid = os.environ.get('TWILIO_ACCOUNT_SID')
        auth_token = os.environ.get('TWILIO_AUTH_TOKEN')
        
        if not account_sid or not auth_token:
            # Twilio 없으면 기본 STUN만
            ice_servers = [
                {"urls": "stun:stun.l.google.com:19302"},
                {"urls": "stun:stun1.l.google.com:19302"},
            ]
        else:
            # Twilio API 호출
            url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Tokens.json"
            credentials = f"{account_sid}:{auth_token}"
            encoded_credentials = base64.b64encode(credentials.encode()).decode()
            
            headers = {
                "Authorization": f"Basic {encoded_credentials}",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            response = requests.post(url, headers=headers, data={"Ttl": 3600})
            response.raise_for_status()
            
            token_data = response.json()
            ice_servers = token_data.get("ice_servers", [])
        
        return web.Response(
            content_type="application/json",
            text=json.dumps({"iceServers": ice_servers})
        )
        
    except Exception as e:
        logging.error(f"ICE 서버 정보 제공 오류: {e}")
        # 에러 시 기본 STUN 제공
        return web.Response(
            content_type="application/json",
            text=json.dumps({
                "iceServers": [
                    {"urls": "stun:stun.l.google.com:19302"},
                    {"urls": "stun:stun1.l.google.com:19302"},
                ]
            })
        )

def create_app():
    """웹 애플리케이션 생성 및 설정"""
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
    
    # 정적 파일 서비스
    app.router.add_static("/static", os.path.join(ROOT, "static"))
    
    # 종료 핸들러 등록
    app.on_shutdown.append(on_shutdown)
    
    return app

if __name__ == "__main__":
    # 환경변수에서 포트 읽기 (Railway, Heroku 등에서 필요)
    port = int(os.environ.get("PORT", 8080))
    
    # 로깅 설정
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    # WebRTC UDP 포트 설정 (Fly.io용)
    setup_webrtc_ports()
    
    logging.info(f"Starting WebRTC Echo Server on port {port}")
    
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=port)
