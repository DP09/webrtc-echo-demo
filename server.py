# server.py
import argparse
import asyncio
import json
import logging
import os
import time
from typing import Set, Dict, Any

from aiohttp import web
from aiohttp_cors import setup as cors_setup, ResourceOptions
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from aiortc.contrib.media import MediaBlackhole

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

        pc = RTCPeerConnection()
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
    
    logging.info(f"Starting WebRTC Echo Server on port {port}")
    
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=port)
