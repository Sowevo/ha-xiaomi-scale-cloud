# Xiaomi Scale Cloud · 米家体重秤云端报告

将米家云端称重报告同步到 Home Assistant，为每个人生成独立的体重、体脂等指标。

S400 Pro 自测支持，S400 推测支持，其他设备可自行选择测试。如果你的设备也能正常使用，欢迎在 [Issues](https://github.com/Sowevo/ha-xiaomi-scale-cloud/issues) 留言，告诉我们设备型号，帮助补充兼容列表。

## 使用效果

扫码登录米家账号，选择秤和要同步的人员，即可为选中的每个人创建一组传感器。人员默认全选，以后出现新人员的称重记录会自动加入；主动取消的人不会自动加回来。

两次刷新之间的多次称重会逐条同步，HA 状态历史使用报告中的称重时间。首次添加只建立最新值；需要补齐以前的记录时，可在“开发者工具 → 动作”调用“导入历史称重”，选择集成并填写人员姓名。导入的是原始状态历史，保留期限由 HA 的 Recorder 配置决定；长期统计需单独处理。

    米家账号 → 手动选择设备
                 └─ 家里的秤
                     ├─ 刷新报告
                     ├─ 人员 A：体重、体脂率、最近测量时间……
                     └─ 人员 B：体重、体脂率、最近测量时间……

- 每位人员提供 **22 个传感器**，每台秤提供一个“刷新报告”按钮。多台秤可分别添加。
- 默认每 **15 分钟**刷新，可调整为 5–1440 分钟；设为 **0** 关闭定时同步，保留手动刷新和自动化触发。
- 指标直接取自云端报告，缺失项目显示“未知”。

## 使用准备

使用 Home Assistant **2026.10.4 或更新版本**，将秤添加到米家，并完成一次称重报告的云端同步。

## 安装：HACS 自定义仓库

1. 打开 **HACS → 右上角菜单 → 自定义仓库**。
2. 填入仓库地址：https://github.com/Sowevo/ha-xiaomi-scale-cloud 。
3. 类型选择 **Integration / 集成**，添加后搜索 **米家体重秤云同步**（也可搜索 Xiaomi Scale Cloud）。
4. 下载集成，重启 **Home Assistant Core**。
5. 按下方“配置”完成扫码登录。

## 安装：手动复制

1. 下载 [Releases](https://github.com/Sowevo/ha-xiaomi-scale-cloud/releases) 中的安装 ZIP，或下载仓库源代码。
2. 只把 custom_components/xiaomi_scale_cloud 文件夹复制到 HA 的 /config/custom_components/。
3. 安装后的文件路径为 /config/custom_components/xiaomi_scale_cloud/manifest.json。
4. 重启 **Home Assistant Core**。

两种方式任选一种。用 HACS 安装后，后续版本也通过 HACS 更新。

## 配置

1. **设置 → 设备与服务 → 添加集成**，搜索 **米家体重秤云同步**（也可搜索 Xiaomi Scale Cloud）。
2. 选择米家账号所在地区。
3. 打开登录页面，用 **米家 App → 扫一扫**扫码并确认，HA 会自动进入设备选择。
4. 从完整设备列表手动选择秤。列表显示设备名称和型号，名称含“秤”或“scale”的设备优先显示。
5. 选择要同步的人员，默认全选；取消不需要的人员后完成添加。

以后可在集成“配置”的“同步人员”中调整：取消勾选会停止同步并禁用已有人员设备，保留历史；重新勾选即可恢复。你的人员选择会保存。

二维码过期后，点击“刷新二维码”重新扫码。关闭登录页后停止等待。

人员列表来自云端称重报告。新成员完成一次称重并上传报告后，就会自动加入。

### 可选：称重后自动刷新

如果你同时安装了小米官方 **Xiaomi Home** 集成，并且其中的秤提供称重事件实体，可以用它及时触发本集成，补齐完整的称重报告。

在 HA 中新建自动化，切换到 YAML 编辑模式，粘贴以下示例。将 `event.your_scale_measurement` 替换为官方集成的称重事件实体，将两处 `button.your_scale_refresh_report` 替换为本集成的“刷新报告”按钮实体。

```yaml
alias: 体重秤称重后刷新云报告
triggers:
  - trigger: state
    entity_id: event.your_scale_measurement
    to: null
conditions:
  - condition: template
    value_template: >-
      {% set previous = as_timestamp(trigger.from_state.state, none)
         if trigger.from_state is not none else none %}
      {% set current = as_timestamp(trigger.to_state.state, none)
         if trigger.to_state is not none else none %}
      {{ previous is not none and current is not none
         and current > previous and 0 <= as_timestamp(now()) - current <= 60 }}
actions:
  - delay: "00:00:30"
  - condition: template
    value_template: "{{ has_value('button.your_scale_refresh_report') }}"
  - action: button.press
    target:
      entity_id: button.your_scale_refresh_report
mode: restart
```

这个示例会过滤启动恢复的旧事件，并在最后一次有效称重事件后等待 **30 秒**再刷新，避免连续事件重复触发。报告上传较慢时，可适当增加等待时间。

确认自动化能正常刷新后，可在本集成的“配置”中将同步间隔设为 **0**，关闭定时同步，改由称重事件触发。若希望在遗漏事件时仍能补齐数据，也可以保留定时同步。

## 传感器

体重、BMI、体脂率、脂肪量、肌肉量、肌肉率、骨骼肌量、身体水分率/量、骨矿物含量/比例、蛋白质率/量、内脏脂肪等级、基础代谢、身体年龄、身体评分、去脂体重、两项阻抗、心率，以及最近测量时间。

“最近测量时间”和历史记录使用报告中的称重时间。

## 账号与隐私

登录会话保存在 HA 配置中，请妥善保管配置和备份。提交反馈时，请将账号和报告中的个人信息脱敏。

## 开发

需要 Python 3.14，建议在独立虚拟环境内运行：

    python -m pip install "homeassistant==2026.10.4" "requests>=2.32.5" "pycryptodome>=3.23.0"
    python -m unittest discover -s tests -v

静态检查：

    ruff check --select F,E9 --exclude _vendor .

## 反馈

请通过 [GitHub Issues](https://github.com/Sowevo/ha-xiaomi-scale-cloud/issues) 提供集成版本、HA 版本、设备英文型号、地区、复现步骤和脱敏错误信息。未验证型号的成功或失败反馈都欢迎。

## 来源与许可证

- [xiaomi-s400](https://github.com/jonathanmullerr/xiaomi-s400)：MIT 许可的认证和加密请求代码来源。本项目增加每实例型号支持及 HA 配置、轮询和实体封装。

本项目使用 MIT 许可证；第三方署名及许可保留在 LICENSE、THIRD-PARTY-NOTICES.md 和集成的 _vendor 目录中。二维码流程原始来源见第三方声明。
