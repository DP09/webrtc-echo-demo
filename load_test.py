#!/usr/bin/env python3
"""
WebRTC Echo 서버 부하 테스트 스크립트
동시에 여러 WebRTC 연결을 생성하여 서버 성능을 테스트합니다.
"""

import asyncio
import aiohttp
import json
import time
import logging
from typing import List
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaBlackhole

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class LoadTestClient:
    def __init__(self, client_id: int, server_url: str):
        self.client_id = client_id
        self.server_url = server_url
        self.pc = None
        self.session = None
        self.connected = False
        self.start_time = None
        self.connect_time = None

    async def connect(self) -> bool:
        """WebRTC 연결 시도"""
        try:
            self.start_time = time.time()
            self.pc = RTCPeerConnection()
            self.session = aiohttp.ClientSession()

            # 가상 오디오 트랙 추가 (실제 마이크 대신)
            # MediaBlackhole 인스턴스 생성 후 audio 트랙 가져오기
            blackhole = MediaBlackhole()
            audio_track = blackhole.audio
            self.pc.addTrack(audio_track)

            # 연결 상태 모니터링
            @self.pc.on("connectionstatechange")
            async def on_connectionstatechange():
                if self.pc.connectionState == "connected" and not self.connected:
                    self.connect_time = time.time()
                    self.connected = True
                    duration = (self.connect_time - self.start_time) * 1000
                    logger.info(f"Client {self.client_id}: Connected in {duration:.1f}ms")

            # Offer 생성 및 전송
            offer = await self.pc.createOffer()
            await self.pc.setLocalDescription(offer)

            # 서버에 offer 전송
            async with self.session.post(
                f"{self.server_url}/offer",
                headers={"Content-Type": "application/json"},
                json={
                    "sdp": self.pc.localDescription.sdp,
                    "type": self.pc.localDescription.type
                }
            ) as response:
                if response.status != 200:
                    raise Exception(f"Server error: {response.status}")
                
                answer_data = await response.json()
                answer = RTCSessionDescription(
                    sdp=answer_data["sdp"],
                    type=answer_data["type"]
                )
                await self.pc.setRemoteDescription(answer)

            return True

        except Exception as e:
            logger.error(f"Client {self.client_id}: Connection failed - {e}")
            await self.cleanup()
            return False

    async def cleanup(self):
        """연결 정리"""
        if self.pc:
            await self.pc.close()
        if self.session:
            await self.session.close()

    async def wait_for_connection(self, timeout: float = 10.0) -> bool:
        """연결 완료까지 대기"""
        end_time = time.time() + timeout
        while time.time() < end_time:
            if self.connected:
                return True
            await asyncio.sleep(0.1)
        return False

class LoadTester:
    def __init__(self, server_url: str = "http://localhost:8080"):
        self.server_url = server_url
        self.clients: List[LoadTestClient] = []

    async def run_test(self, num_clients: int, concurrent_limit: int = 50):
        """부하 테스트 실행"""
        logger.info(f"Starting load test with {num_clients} clients")
        logger.info(f"Concurrent connection limit: {concurrent_limit}")
        
        start_time = time.time()
        successful_connections = 0
        failed_connections = 0

        # 클라이언트들을 배치 단위로 처리
        for batch_start in range(0, num_clients, concurrent_limit):
            batch_end = min(batch_start + concurrent_limit, num_clients)
            batch_size = batch_end - batch_start
            
            logger.info(f"Processing batch {batch_start}-{batch_end-1} ({batch_size} clients)")
            
            # 배치 내 클라이언트들 생성
            batch_clients = []
            for i in range(batch_start, batch_end):
                client = LoadTestClient(i, self.server_url)
                batch_clients.append(client)
                self.clients.append(client)

            # 동시 연결 시도
            batch_start_time = time.time()
            connection_tasks = [client.connect() for client in batch_clients]
            results = await asyncio.gather(*connection_tasks, return_exceptions=True)

            # 연결 완료 대기
            wait_tasks = [client.wait_for_connection() for client in batch_clients]
            connection_results = await asyncio.gather(*wait_tasks, return_exceptions=True)

            # 결과 집계
            batch_successful = sum(1 for result in connection_results if result is True)
            batch_failed = batch_size - batch_successful
            
            successful_connections += batch_successful
            failed_connections += batch_failed
            
            batch_duration = time.time() - batch_start_time
            logger.info(f"Batch completed in {batch_duration:.1f}s: {batch_successful} success, {batch_failed} failed")

            # 배치 간 잠시 대기 (서버 부하 분산)
            if batch_end < num_clients:
                await asyncio.sleep(1)

        total_duration = time.time() - start_time
        
        # 최종 결과 출력
        logger.info("=" * 50)
        logger.info("LOAD TEST RESULTS")
        logger.info("=" * 50)
        logger.info(f"Total clients: {num_clients}")
        logger.info(f"Successful connections: {successful_connections}")
        logger.info(f"Failed connections: {failed_connections}")
        logger.info(f"Success rate: {(successful_connections/num_clients)*100:.1f}%")
        logger.info(f"Total test duration: {total_duration:.1f}s")
        logger.info(f"Average time per client: {(total_duration/num_clients)*1000:.1f}ms")

        return {
            "total_clients": num_clients,
            "successful": successful_connections,
            "failed": failed_connections,
            "success_rate": (successful_connections/num_clients)*100,
            "total_duration": total_duration
        }

    async def cleanup_all(self):
        """모든 클라이언트 정리"""
        logger.info(f"Cleaning up {len(self.clients)} clients...")
        cleanup_tasks = [client.cleanup() for client in self.clients]
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)

async def main():
    """메인 테스트 함수"""
    import argparse
    
    parser = argparse.ArgumentParser(description="WebRTC Echo Server Load Test")
    parser.add_argument("--clients", type=int, default=50, help="Number of clients to simulate")
    parser.add_argument("--concurrent", type=int, default=10, help="Concurrent connection limit")
    parser.add_argument("--url", default="http://localhost:8080", help="Server URL")
    
    args = parser.parse_args()
    
    tester = LoadTester(args.url)
    
    try:
        results = await tester.run_test(args.clients, args.concurrent)
        
        # 성능 평가
        if results["success_rate"] >= 95:
            logger.info("✅ EXCELLENT: 95%+ success rate")
        elif results["success_rate"] >= 80:
            logger.info("✅ GOOD: 80%+ success rate")
        elif results["success_rate"] >= 60:
            logger.info("⚠️ FAIR: 60%+ success rate - consider optimization")
        else:
            logger.info("❌ POOR: <60% success rate - needs improvement")
            
    finally:
        await tester.cleanup_all()

if __name__ == "__main__":
    asyncio.run(main()) 