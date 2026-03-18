#!/usr/bin/env python3
"""
全自动部署脚本 - 报告生成系统到 Render.com
此脚本将指导你完成从零到部署的完整流程
"""

import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

def run_cmd(cmd, cwd=None, check=True):
    """运行命令并返回结果"""
    print(f"▶️  执行: {cmd}")
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"❌ 命令失败: {result.stderr}")
        sys.exit(1)
    return result

def step1_check_environment():
    """步骤1：检查环境"""
    print("=" * 60)
    print("步骤 1: 检查环境")
    print("=" * 60)
    
    # 检查是否在项目目录
    if not Path("app.py").exists():
        print("❌ 错误：请在项目根目录（包含 app.py 的目录）运行此脚本")
        sys.exit(1)
    
    print("✅ 项目目录正确")
    
    # 检查 Git
    result = run_cmd("git --version", check=False)
    if result.returncode != 0:
        print("❌ Git 未安装。请先安装 Git：https://git-scm.com")
        sys.exit(1)
    print("✅ Git 已安装")
    
    # 检查 Python 3
    result = run_cmd("python3 --version", check=False)
    if result.returncode == 0:
        print("✅ Python 3 已安装")
    else:
        print("⚠️  Python 3 可能未安装，但 Docker 部署不需要")

def step2_github_setup():
    """步骤2：GitHub 设置"""
    print("\n" + "=" * 60)
    print("步骤 2: GitHub 仓库设置")
    print("=" * 60)
    
    # 检查是否已有 .git 目录
    if Path(".git").exists():
        print("📦 Git 仓库已存在，跳过初始化")
    else:
        print("📦 初始化 Git 仓库...")
        run_cmd("git init")
        run_cmd("git add .")
        run_cmd('git commit -m "feat: 部署FastAPI报告生成系统到Render"')
        print("✅ Git 仓库初始化完成")
    
    # 检查远程仓库
    result = run_cmd("git remote get-url origin", check=False)
    if result.returncode == 0 and result.stdout.strip():
        print(f"🌐 已配置远程仓库：{result.stdout.strip()}")
        return result.stdout.strip()
    
    print("\n📝 现在需要创建 GitHub 仓库")
    print("\n请按以下步骤操作：")
    print("1. 访问 https://github.com/new")
    print("2. 填写仓库名称（例如：report-automation）")
    print("3. 选择 Public 或 Private（推荐 Public）")
    print("4. 不要初始化 README、.gitignore 或 license")
    print("5. 点击 'Create repository'")
    print("6. 复制仓库的 HTTPS URL（格式：https://github.com/用户名/仓库名.git）")
    
    webbrowser.open("https://github.com/new")
    
    while True:
        repo_url = input("\n🔗 请输入你的 GitHub 仓库 URL: ").strip()
        if repo_url and ("github.com" in repo_url or "git@" in repo_url):
            break
        print("❌ 请输入有效的 GitHub 仓库 URL")
    
    # 配置远程仓库
    run_cmd(f"git remote add origin {repo_url}")
    run_cmd("git branch -M main")
    print("✅ 远程仓库已配置")
    
    return repo_url

def step3_push_to_github():
    """步骤3：推送到 GitHub"""
    print("\n" + "=" * 60)
    print("步骤 3: 推送到 GitHub")
    print("=" * 60)
    
    print("🚀 推送代码到 GitHub...")
    result = run_cmd("git push -u origin main", check=False)
    
    if result.returncode != 0:
        print("⚠️  首次推送可能需要 GitHub 认证")
        print("\n如果出现认证提示，请按以下步骤操作：")
        print("1. 如果要求用户名和密码，请使用 GitHub Personal Access Token")
        print("2. 生成 Token：https://github.com/settings/tokens/new")
        print("3. 权限选择：repo（完全控制仓库）")
        print("4. 将 Token 作为密码输入")
        print("\n重试推送命令...")
        run_cmd("git push -u origin main")
    
    print("✅ 代码已成功推送到 GitHub")

def step4_deploy_to_render():
    """步骤4：部署到 Render"""
    print("\n" + "=" * 60)
    print("步骤 4: 部署到 Render.com")
    print("=" * 60)
    
    print("\n🎯 现在需要登录 Render 并创建服务")
    print("\n请按以下步骤操作：")
    print("1. 访问 https://dashboard.render.com")
    print("2. 使用 GitHub 登录（推荐）")
    print("3. 点击 'New +' → 'Web Service'")
    print("4. 在 'Connect a repository' 部分，选择你刚刚创建的仓库")
    print("5. 配置服务：")
    print("   • Name: report-automation（可自定义）")
    print("   • Region: Singapore (sin)（推荐亚洲节点，国内访问快）")
    print("   • Branch: main")
    print("   • Runtime: Python 3")
    print("   • Build Command: pip install -r requirements.txt")
    print("   • Start Command: uvicorn app:app --host 0.0.0.0 --port $PORT")
    print("   • Instance Type: Free")
    print("6. 点击 'Create Web Service'")
    print("\n正在打开 Render 控制台...")
    
    webbrowser.open("https://dashboard.render.com")
    
    print("\n⏳ 等待构建完成（约 2-5 分钟）...")
    print("\n构建完成后，你将获得一个访问链接，格式如：")
    print("   https://report-automation.onrender.com")
    
    input("\n📝 请按 Enter 键继续，当你获得 Render 链接后，我会帮你验证部署...")

def step5_verify_deployment():
    """步骤5：验证部署"""
    print("\n" + "=" * 60)
    print("步骤 5: 验证部署")
    print("=" * 60)
    
    while True:
        render_url = input("\n🔗 请输入你的 Render 服务链接（如 https://xxx.onrender.com）: ").strip()
        if render_url and "onrender.com" in render_url:
            break
        print("❌ 请输入有效的 Render 链接（应包含 onrender.com）")
    
    print(f"\n✅ 你的报告生成系统已部署到：")
    print(f"   {render_url}")
    print(f"\n📋 测试链接：")
    print(f"   1. 主界面: {render_url}")
    print(f"   2. API 端点: {render_url}/generate")
    print(f"\n🎉 部署完成！现在你可以：")
    print(f"   • 分享链接给同事使用")
    print(f"   • 集成到其他系统中")
    print(f"   • 通过 GitHub 推送代码自动更新")

def main():
    """主函数"""
    print("🦐 报告生成系统全自动部署脚本")
    print("=" * 60)
    print("此脚本将指导你完成从代码到生产的完整部署流程")
    print("需要在 GitHub 和 Render 登录时，我会打开浏览器并指导你操作")
    print("=" * 60)
    
    # 确保在项目目录
    project_path = Path("/Users/qyh/Desktop/daily file/Internship/北京算路科技有限公司/报告自动化")
    if not Path("app.py").exists() and project_path.exists():
        os.chdir(project_path)
        print(f"📁 切换到项目目录: {project_path}")
    
    # 执行各步骤
    step1_check_environment()
    repo_url = step2_github_setup()
    step3_push_to_github()
    step4_deploy_to_render()
    step5_verify_deployment()
    
    print("\n" + "=" * 60)
    print("🎊 部署流程完成！")
    print("=" * 60)
    print("\n📞 如需进一步协助，请随时联系我。")
    print("\n⚠️  注意事项：")
    print("   • Render 免费实例在闲置后会休眠，首次访问可能需要 30-60 秒唤醒")
    print("   • 如需更高性能或自定义域名，可升级到付费套餐")
    print("   • 代码更新只需推送到 GitHub，Render 会自动重新部署")

if __name__ == "__main__":
    main()