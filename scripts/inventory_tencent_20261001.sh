#!/bin/bash
# 只读家产盘点 —— 腾讯云 SSE_Market (2026-10-01)
echo "########## 1. 主机基本 ##########"
hostname; uptime
df -h / | tail -1
free -h | head -2
nproc
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo "no-gpu"

echo "########## 2. 端口全景 (ss -tlnp) ##########"
ss -tlnp | awk 'NR==1 || /LISTEN/' | sort -k4

echo "########## 3. Docker 容器 (docker ps -a) ##########"
docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

echo "########## 4. Docker 镜像 ##########"
docker images --format '{{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}' | sort

echo "########## 5. Docker 卷 ##########"
docker volume ls --format '{{.Name}}' | head -40

echo "########## 6. Docker 网络 ##########"
docker network ls --format '{{.Name}}\t{{.Driver}}'

echo "########## 7. systemd 自定义服务 ##########"
systemctl list-units --type=service --state=running --no-pager --no-legend | grep -Ev 'systemd|dbus|ssh|cron|rsyslog|getty|polkit|network|multipath|qemu|acpid|atd|irqbalance|packagekit|udisks|unattended|wap|chrony|sap|tower|omsa|ecs|barad|tcpdump|kmods' || true

echo "########## 8. crontab ##########"
crontab -l 2>/dev/null

echo "########## 9. k3s 全量 ##########"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl get nodes -o wide 2>/dev/null
kubectl get pods -A -o wide 2>/dev/null
kubectl get svc -A 2>/dev/null
kubectl get deploy,sts,ds,rc -A 2>/dev/null | head -30
kubectl get netpol,quota,limitrange -A 2>/dev/null

echo "########## 10. /root 目录 ##########"
du -sh /root/* 2>/dev/null | sort -rh | head -25

echo "########## 11. nginx vhost 摘要 ##########"
ls /etc/nginx/conf.d/ 2>/dev/null
grep -h 'listen\|server_name\|proxy_pass' /etc/nginx/conf.d/*.conf 2>/dev/null | grep -v '^\s*#' | head -40

echo "########## 12. frps 状态与代理通道 ##########"
systemctl is-active frps 2>/dev/null
cat /opt/frp/frps.toml 2>/dev/null | grep -v 'auth.token\|password'

echo "########## 13. 尾进程 (非系统) ##########"
ps aux --sort=-%mem | head -18
