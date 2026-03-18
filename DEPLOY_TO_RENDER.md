# 一键部署到 Render（国内可访问）

本指南将帮助你将报告生成系统部署到 Render.com，该平台在国内无需 VPN 即可访问，并提供免费套餐。

## 📋 准备工作

1. **GitHub 账号**：确保你拥有 GitHub 账号（如果没有，请前往 https://github.com 注册）
2. **Render 账号**：确保你拥有 Render 账号（如果没有，请前往 https://render.com 注册，建议使用 GitHub 登录）
3. **本地环境**：确保已安装 Git 和 Python 3.11+

## 🚀 自动化部署步骤

### 步骤 1：初始化 Git 仓库并推送至 GitHub

打开终端，执行以下命令：

```bash
# 进入项目目录
cd "/Users/qyh/Desktop/daily file/Internship/北京算路科技有限公司/报告自动化"

# 初始化 Git 仓库
git init
git add .
git commit -m "feat: 部署FastAPI报告生成系统到Render"

# 在 GitHub 上创建新仓库（手动操作）
# 1. 访问 https://github.com/new
# 2. 仓库名称填写 "report-automation"（或其他你喜欢的名字）
# 3. 选择 Public 或 Private
# 4. 不要初始化 README、.gitignore 或 license
# 5. 点击 "Create repository"

# 将本地仓库连接到 GitHub
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git branch -M main
git push -u origin main
```

### 步骤 2：在 Render 上创建 Web 服务

1. **登录 Render**：访问 https://dashboard.render.com，使用 GitHub 登录
2. **创建新服务**：点击 "New +" → "Web Service"
3. **连接仓库**：点击 "Connect account" 或 "Configure account" 授权 Render 访问你的 GitHub 仓库
4. **选择仓库**：在仓库列表中找到并选择你刚刚创建的仓库
5. **配置服务**：
   - **Name**：`report-automation`（自动填充，可修改）
   - **Root Directory**：留空（自动识别）
   - **Region**：选择 `Singapore (sin)` 或 `Oregon (us-west)`（亚洲节点访问更快）
   - **Branch**：`main`
   - **Runtime**：`Python 3`
   - **Build Command**：`pip install -r requirements.txt`
   - **Start Command**：`uvicorn app:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**：选择 `Free`

6. **高级设置**（点击 "Advanced"）：
   - 确保 "Auto-Deploy" 开启
   - 环境变量无需额外添加

7. **创建服务**：点击 "Create Web Service"

### 步骤 3：等待构建并获取访问链接

Render 将自动开始构建，大约需要 2-5 分钟。构建完成后，你将看到类似以下的访问链接：

```
https://report-automation.onrender.com
```

## 🔧 验证部署

1. 访问你的 Render 服务链接（如 `https://report-automation.onrender.com`）
2. 应该看到报告生成系统的 Web 界面
3. 尝试填写数据并点击 "生成最终 Word"，检查是否能正常下载文件

## ⚠️ 常见问题

### 1. 构建失败
- **错误信息**："ModuleNotFoundError: No module named 'fastapi'"
  - 确保 `requirements.txt` 文件存在且包含正确的依赖
  - 检查 Build Command 是否为 `pip install -r requirements.txt`

- **错误信息**："Template file not found"
  - 确保 `.docx` 文件已包含在 Git 仓库中（未被 `.gitignore` 排除）
  - 检查 `app.py` 中的 `TEMPLATE_PATH` 是否为正确的相对路径

### 2. 访问超时或缓慢
- Render 免费实例在闲置后会休眠，首次访问可能需要 30-60 秒唤醒时间
- 选择新加坡（Singapore）区域可改善国内访问速度

### 3. CORS 问题
- 如果前端调用 API 出现跨域错误，请确认 `app.py` 中的 CORS 配置已生效
- 可尝试将 `allow_origins=["*"]` 改为你的 Render 域名

### 4. 文件下载问题
- 确保生成的文件名不含特殊字符
- 检查服务器日志（Render Dashboard → Logs）查看具体错误

## 📞 获取帮助

如果遇到问题，可以：

1. 检查 Render Dashboard 中的 Build Logs 和 Runtime Logs
2. 在 Render 文档中搜索常见问题：https://render.com/docs
3. 联系我获取进一步协助

## 🔄 更新部署

当你修改代码后，只需将更改推送到 GitHub：

```bash
git add .
git commit -m "更新描述"
git push origin main
```

Render 会自动检测更改并重新部署。

---

部署完成后，请将服务链接保存并分享给需要使用的同事。系统现已公开可用，无需 VPN 即可在国内访问。