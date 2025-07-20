# server.py
import argparse
import asyncio
import json
import logging
import os
from typing import Set

from aiohttp import web
from aiohttp_cors import setup as cors_setup, ResourceOptions
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from aiortc.contrib.media import MediaBlackhole

ROOT = os.path.dirname(__file__)

# 전역 변수를 더 명확하게 타입 힌트와 함께
pcs: Set[RTCPeerConnection] = set()

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
    try:
        params = await request.json()
        offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

        pc = RTCPeerConnection()
        pcs.add(pc)
        logging.info(f"Created PeerConnection. Total connections: {len(pcs)}")

        @pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                logging.info("Received audio track")
                pc.addTrack(AudioEchoTrack(track))

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            logging.info(f"Connection state changed: {pc.connectionState}")
            if pc.connectionState in ["failed", "closed"]:
                if pc in pcs:
                    pcs.discard(pc)
                    logging.info(f"Removed PeerConnection. Total connections: {len(pcs)}")

        await pc.setRemoteDescription(offer)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        return web.Response(
            content_type="application/json",
            text=json.dumps({
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type
            })
        )
    
    except Exception as e:
        logging.error(f"Error in offer handling: {e}")
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
