FROM python:3.9-slim

# 시스템 패키지 설치 (aiortc + coturn)
RUN apt-get update && apt-get install -y \
    build-essential \
    libffi-dev \
    libssl-dev \
    coturn \
    && rm -rf /var/lib/apt/lists/*

# 작업 디렉토리 설정
WORKDIR /app

# requirements 먼저 복사 (Docker 캐시 최적화)
COPY requirements.txt .

# Python 패키지 설치
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 코드 복사
COPY . .

# Railway용 간단한 coturn 설정
RUN echo "listening-port=3478\n\
external-ip=\n\
user=webrtc:webrtc123\n\
lt-cred-mech\n\
realm=railway.app\n\
log-file=/var/log/turnserver.log" > /etc/turnserver.conf

# 시작 스크립트 생성 (coturn에 Public IP 동적 설정)
RUN echo '#!/bin/bash\n\
# Railway Public Domain이 CUSTOM_TURN_SERVER로 주입되면, turnserver.conf의 external-ip를 동적으로 설정\n\
if [ -n "$CUSTOM_TURN_SERVER" ]; then\n\
  # 기존 external-ip= 라인을 찾아 동적 값으로 대체\n\
  sed -i "s|^external-ip=.*|external-ip=$CUSTOM_TURN_SERVER|" /etc/turnserver.conf\n\
fi\n\
# TURN 서버 백그라운드 시작\n\
turnserver -c /etc/turnserver.conf &\n\
# WebRTC 서버 시작\n\
python server.py' > /app/start.sh

RUN chmod +x /app/start.sh

# 포트 노출
EXPOSE 8080

# 환경변수 설정 (빈 값으로 초기화, Railway에서 주입 시 덮어씀)
ENV CUSTOM_TURN_SERVER=""
ENV CUSTOM_TURN_USER=""
ENV CUSTOM_TURN_PASS=""
ENV TWILIO_ACCOUNT_SID=""
ENV TWILIO_AUTH_TOKEN=""

# 통합 시작
CMD ["/app/start.sh"] 