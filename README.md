'''
#version: '3.8'

services:
  app:
    image: yuntian123456/tickflow-stock-panel:latest  
    container_name: TickFlow_Stock_Panel
    restart: unless-stopped
    ports:
      - "${PORT:-3018}:3018"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    env_file:
      - .env
    environment:
      - DATA_DIR=/app/data
      - CODEX_DOCKER_HOST=host.docker.internal
    volumes:
      - ./data:/app/data # 持久化保存股票数据与配置
      # 如果需要挂载自定义 tiers.yaml，取消下行注释即可：
      # - ./tiers.yaml:/app/tiers.yaml:ro
      '''