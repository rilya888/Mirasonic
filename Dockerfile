FROM python:3.12-slim AS app

# ffmpeg repackages googlevideo's fragmented mp4 into ADTS — without it
# clients crash on seek (see the long comment in main.py).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py subsonic.py library.py spotify_import.py spotify_sync.py \
     ranking.py listenbrainz_client.py music_agent.py ./

# 0.0.0.0, not 127.0.0.1: inside a container loopback is unreachable from
# outside even with -p 8080:8080. Isolation comes from how the port is
# published on the host, not from the bind address in here.
ENV HOST=0.0.0.0 PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host $HOST --port $PORT"]

# The Spotify mix mirror (spotify_sync.py) drives exportify.net in Chromium —
# ~400 MB the server itself has no use for, so it lives in its own image.
# The default target is `app`: compose names it for worker and agent.
FROM app AS spotify-sync
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install --no-cache-dir playwright==1.63.0 \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*
CMD ["python", "spotify_sync.py", "daemon"]
