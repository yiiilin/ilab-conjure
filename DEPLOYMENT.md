# iLab Conjure Compose 部署说明

## 当前部署

- 项目目录：`/path/to/ilab-conjure`
- 本地入口：`http://127.0.0.1:13221`
- 容器端口：`8787`
- 持久化目录：`/path/to/ilab-conjure/data`
- API Key：不写入 Compose；在 WebUI 的“系统设置 → API 设置”中配置

## 常用命令

```bash
cd /path/to/ilab-conjure

docker compose ps
docker compose logs --since=10m
docker compose restart
docker compose stop
docker compose start
```

## 更新

这是源码构建部署，不是远程镜像部署：

```bash
cd /path/to/ilab-conjure
git pull --ff-only
docker compose build
docker compose up -d
```

更新前建议备份 `data/`。部署文件 `Dockerfile`、`compose.yaml`、`.dockerignore` 和 `.env` 是本机补充文件；如果未来上游加入同名文件，先比较后再更新。

## 备份

至少备份：

```text
/path/to/ilab-conjure/data/
/path/to/ilab-conjure/compose.yaml
/path/to/ilab-conjure/Dockerfile
/path/to/ilab-conjure/.env
```

`data/` 包含 API 供应商设置、SQLite 数据库、输入图、输出图、图库和提示词模板。不要公开或提交到 Git。

## OpenResty 反代要点

将域名上游指向：

```text
http://127.0.0.1:13221
```

生图请求可能耗时较长，建议反代读写超时至少 900 秒，并允许足够大的上传请求体。服务本身只监听回环地址，不应改成 `0.0.0.0` 公网暴露。
