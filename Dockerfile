FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY . .

# 暴露端口（Koyeb会自动检测）
EXPOSE 8000

# 启动命令
CMD ["python", "bot.py"]
