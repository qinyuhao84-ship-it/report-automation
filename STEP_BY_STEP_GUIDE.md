# 全自动部署：Step-by-Step 指引

## 🎯 目标
将你的 FastAPI 报告生成系统部署到 Render.com，获得一个国内可访问的公开链接。

## ⏱️ 预计时间：10-15 分钟

## 📋 前置条件
- ✅ GitHub 账号（如果没有：https://github.com/signup）
- ✅ Render 账号（如果没有：https://render.com/signup，建议用 GitHub 登录）

## 🚀 部署流程

### 步骤 1：创建 GitHub 仓库
1. **访问** https://github.com/new
2. **填写仓库信息**：
   - Repository name: `report-automation`（或其他名称）
   - Visibility: `Public`（推荐）或 `Private`
   - **重要**：不要勾选 "Initialize this repository with:"
   - 点击 "Create repository"
3. **复制仓库 URL**：
   - 你会看到类似 `https://github.com/你的用户名/report-automation.git` 的 URL
   - 复制这个 URL

### 步骤 2：推送代码到 GitHub
1. **打开终端**，进入项目目录：
   ```bash
   cd "/Users/qyh/Desktop/daily file/Internship/北京算路科技有限公司/报告自动化"
   ```

2. **设置远程仓库并推送**：
   ```bash
   # 如果尚未初始化 Git
   git init
   git add .
   git commit -m "feat: 部署报告生成系统"
   
   # 设置远程仓库（用你复制的 URL 替换下面的链接）
   git remote add origin https://github.com/你的用户名/report-automation.git
   git branch -M main
   git push -u origin main
   ```

3. **如果要求认证**：
   - 用户名：你的 GitHub 用户名
   - 密码：使用 **GitHub Personal Access Token**（不是普通密码）
   - 生成 Token：https://github.com/settings/tokens/new
   - 权限选择：`repo`（完全控制仓库）
   - 复制 Token 并作为密码输入

### 步骤 3：部署到 Render
1. **访问** https://dashboard.render.com
2. **登录**：使用 GitHub 登录（推荐）
3. **创建 Web 服务**：
   - 点击 "New +" → "Web Service"
   - 在 "Connect a repository" 部分，点击 "Connect account" 授权
   - 选择你刚刚创建的 `report-automation` 仓库
4. **配置服务**：
   - **Name**: `report-automation`（自动填充）
   - **Region**: `Singapore (sin)`（亚洲节点，国内访问快）
   - **Branch**: `main`
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: `Free`
5. **创建服务**：
   - 点击 "Create Web Service"
   - 等待构建完成（约 2-5 分钟）

### 步骤 4：获取访问链接
构建完成后，你会看到：
```
Your service is live 🎉
https://report-automation.onrender.com
```

### 步骤 5：验证部署
1. **访问你的 Render 链接**
2. **测试功能**：
   - 填写表单数据
   - 点击 "生成最终 Word"
   - 检查是否能正常下载 `.docx` 文件

## 🔧 故障排除

### 问题 1：Git 推送失败
```
fatal: Authentication failed
```
- 解决方案：使用 GitHub Personal Access Token 作为密码

### 问题 2：Render 构建失败
- 检查 Build Logs 中的错误信息
- 常见原因：依赖安装失败、文件缺失
- 确保 `.docx` 模板文件已包含在仓库中

### 问题 3：应用启动失败
- 检查 Runtime Logs
- 确认 `requirements.txt` 包含 `fastapi`, `uvicorn`, `pydantic`

### 问题 4：国内访问慢
- 确保选择了 `Singapore (sin)` 区域
- Render 免费实例休眠后首次访问需 30-60 秒唤醒

## 📞 获取帮助

如果遇到问题，请：
1. 复制错误信息发给我
2. 或提供 Render Dashboard 的截图

## 🔄 更新部署

未来更新代码只需：
```bash
git add .
git commit -m "更新描述"
git push origin main
```
Render 会自动重新部署。

---

完成以上步骤后，你将拥有一个公开可访问的报告生成系统，国内无需 VPN 即可使用！