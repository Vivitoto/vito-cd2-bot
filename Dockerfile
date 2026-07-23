FROM python:3.9-slim

WORKDIR /app

# 安装运行依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 放入固定版本的官方接口协议代码、主程序和示例配置
COPY clouddrive_pb2.py clouddrive_pb2_grpc.py ./
COPY app.py .
COPY VERSION .
COPY download-routes.example.yml .
RUN mkdir -p /config

EXPOSE 5000

CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5000", "app:app"]
