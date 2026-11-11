# Model Relay 8.7.1 构建依赖修复

8.7.0 部署在 Dockerfile 的 COPY requirements.txt ./ 阶段失败，因为交付包根目录没有该文件。本版补齐与 Dockerfile 同级的 requirements.txt，固定源码实际使用的 10 项直接依赖版本，包括 API/Worker、multipart 文件接收、Excel 编译及 google-genai SDK。

本版以原始 8.6.0 上传包重新复制为基线，重新应用已审查的 8.7.0 改造差异，再加入依赖修复。与 8.7.0 相比，应用代码仅更新 API_VERSION 为 8.7.1；思考能力映射、配置编译版本、路由、缓存及恢复行为保留。

部署时将本包 model-relay 目录中的内容更新到原仓库。确认 requirements.txt 和 Dockerfile 同级，再重新部署 API 与 Worker。无需手工在 Railway 执行 pip install，Dockerfile 会自动安装依赖。无需修改既有环境变量或新增数据库迁移。

验证在全新 Python 3.12 隔离环境中完成：requirements 全量安装成功，pip check 无依赖冲突；应用编译、API/Worker 导入、6 个工作簿供应离线编译、Gemini SDK 创建与关闭通过。Uvicorn 本地 HTTP 检查通过，/health 返回 8.7.1，OpenAPI 包含 prefer/require 思考策略。

HTTP 检查关闭了外部服务初始化，未连接真实数据库或模型供应。当前环境没有 Docker 可执行程序，未将上述检查表述为完整 Docker 镜像构建或 Railway 部署成功。8.7.0 说明中的依赖缺件记录在本版已修复；其他历史验证范围以原记录为准。本版没有新增测试文件。
