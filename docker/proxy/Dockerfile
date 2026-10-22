FROM python:3.12-slim
RUN pip install --no-cache-dir "dnspython>=2.6"
WORKDIR /app
COPY volatiledns /app/volatiledns
ENTRYPOINT ["python", "-m", "volatiledns"]
