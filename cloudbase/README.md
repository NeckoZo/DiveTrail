# 匿名反馈接口

桌面应用的匿名反馈使用腾讯云 CloudBase。它在用户点击「提交」时进行匿名身份验证，说明最多 1,000 字，可单独发送，也可附带最近 24 小时或全部保留的脱敏采集事件。预览只在本机生成。战绩记录和截图无需登录。

自托管时在自己的 CloudBase 环境开启匿名认证，按文件名顺序应用 `migrations/` 中的 SQL，并在 `matchlog/community.py` 配置公开网关。部署后，用合成数据分别验证直接提交、重复提交及匿名身份拒绝读取。服务端管理实例、数据库凭据和管理密钥留在管理员设备。

提交回执可关联管理员侧的反馈记录。后台管理与匿名提交使用独立的权限。旧客户端的社区统计迁移保留在本目录，1.0 已移除社区统计入口。

- [腾讯云匿名登录](https://docs.cloudbase.net/http-api/auth/auth-sign-in-anonymously)
- [腾讯云数据库函数](https://docs.cloudbase.net/database/postgresql/rpc)
