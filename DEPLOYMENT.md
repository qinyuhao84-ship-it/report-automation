# 部署指南

本系统是一个基于 FastAPI 的自动化报告生成服务，使用 Docker 或 Procfile 均可部署到主流云平台。

## 文件说明

- `app.py` - 主应用文件（已添加 CORS 支持）
- `requirements.txt` - Python 依赖
- `Dockerfile` - 容器化构建文件
- `Procfile` - 平台进程定义（用于 Render、Railway 等）
- `0315-浙江达航数据技术有限公司-自证-初版.docx` - Word 模板文件
- `schema.json` - 数据结构示例（可选）
- `.gitignore` - 忽略虚拟环境等临时文件

## 部署前准备

1. 将本目录初始化为 Git 仓库（若尚未）：
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   ```

2. 在 GitHub 上创建新仓库，并将本地仓库推送到 GitHub：
   ```bash
   git remote add origin <你的仓库URL>
   git branch -M main
   git push -u origin main
   ```

## 平台推荐

### 1. Railway（推荐，免费额度充足）
- 网址：https://railway.app
- 特点：支持自动从 GitHub 部署，内置数据库、日志、监控
- 免费额度：每月 5 美元信用，足以运行小型应用

**部署步骤：**
1. 注册 Railway 账号（可使用 GitHub 登录）
2. 点击 “New Project” → “Deploy from GitHub repo”
3. 选择你的仓库，Railway 会自动检测 `Dockerfile` 或 `Procfile` 并开始构建
4. 部署完成后，Railway 会分配一个公有 URL（如 `https://xxx.up.railway.app`）
5. 访问该 URL 即可使用报告生成界面

### 2. Render（免费套餐，每月 750 小时）
- 网址：https://render.com
- 特点：同样支持 GitHub 自动部署，提供免费 Web 服务

**部署步骤：**
1. 注册 Render 账号
2. 点击 “New +” → “Web Service”
3. 连接你的 GitHub 仓库
4. 选择 “Root directory”，Render 会自动识别 `Dockerfile` 或 `Procfile`
5. 在免费套餐中，选择 “Free” 实例类型
6. 点击 “Create Web Service”，等待构建完成
7. 访问分配的 `.onrender.com` 域名即可

### 3. Vercel（更适合前端，但也可部署 Python）
- Vercel 对 Python 支持有限，需要配置 `vercel.json`，不建议首选。

## 跨域（CORS）说明

应用中已添加 CORS 中间件，允许所有来源（`allow_origins=["*"]`）访问。在生产环境中，若已知前端域名，建议将其替换为具体域名以提高安全性。

## 模板文件管理

Word 模板文件 `0315-浙江达航数据技术有限公司-自证-初版.docx` 已通过 `Dockerfile` 和 `Procfile` 部署流程包含在镜像/容器中。应用启动后，模板文件位于容器工作目录，代码直接通过相对路径读取。

**注意事项：**
- 如需更换模板，请替换同名文件并重新部署。
- 模板文件名中的中文在容器内不会导致问题，但若在构建过程中出现编码错误，可考虑将文件重命名为英文（同时修改 `app.py` 中的 `TEMPLATE_PATH`）。

## 本地测试部署

### 使用 Docker（推荐）
```bash
# 构建镜像
docker build -t report-automation .

# 运行容器
docker run -p 8000:8000 report-automation
```
访问 http://localhost:8000 查看界面。

### 使用传统方式
```bash
# 创建虚拟环境（可选）
python -m venv venv
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 启动服务
uvicorn app:app --host 0.0.0.0 --port 8000
```

## 故障排除

- **构建失败**：检查 `requirements.txt` 中依赖是否兼容；确保 Dockerfile 中 Python 版本为 3.11。
- **模板文件找不到**：确认 `.docx` 文件已正确复制到容器内（可通过 `docker exec` 进入容器查看）。
- **CORS 问题**：若前端仍报跨域错误，检查 `app.py` 中 CORS 配置是否生效，或尝试将 `allow_origins` 改为前端实际域名。

## 后续优化建议

1. **安全性**：将 `allow_origins` 从 `["*"]` 改为具体前端域名。
2. **性能**：考虑使用 Nginx 反向代理、Gunicorn 多进程。
3. **存储**：若生成的文件需要持久化，可挂载云存储卷（如 AWS S3、Railway Volumes）。

---

部署完成后，请将生成的服务 URL 分享给需要使用的同事或集成到前端应用中。