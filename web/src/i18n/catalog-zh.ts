import type {
  AutomationBlueprint,
  AutomationBlueprintField,
} from "@/lib/automation-blueprints";
import type {
  CronJob,
  MessagingPlatform,
  MessagingPlatformEnvVar,
} from "@/lib/api";

export function isZhDocument(): boolean {
  return (
    typeof document !== "undefined" &&
    /^zh\b/i.test(document.documentElement.lang)
  );
}

/**
 * Resolve active locale, falling back to "en" in bare component tests that
 * never mounted <I18nProvider> (so document.documentElement.lang is "").
 */
export function effectiveLocale(locale: string): string {
  if (
    locale === "zh" &&
    typeof document !== "undefined" &&
    !document.documentElement.lang
  ) {
    return "en";
  }
  return locale;
}

export interface BlueprintStrings {
  setUp: string;
  cancel: string;
  scheduleIt: string;
  pluginPrefix: string;
  loadErrorPrefix: string;
  loading: string;
  empty: string;
  scheduledToast: (title: string, when: string) => string;
}

const BLUEPRINT_STRINGS_EN: BlueprintStrings = {
  setUp: "Set up",
  cancel: "Cancel",
  scheduleIt: "Schedule it",
  pluginPrefix: "plugin: ",
  loadErrorPrefix: "Couldn't load blueprints: ",
  loading: "Loading blueprints…",
  empty: "No automation blueprints available.",
  scheduledToast: (title, when) => `${title} scheduled${when}`,
};

const BLUEPRINT_STRINGS_ZH: BlueprintStrings = {
  setUp: "使用模板",
  cancel: "取消",
  scheduleIt: "安装定时任务",
  pluginPrefix: "插件：",
  loadErrorPrefix: "无法加载自动化模板：",
  loading: "正在加载自动化模板…",
  empty: "暂无可用的自动化模板。",
  scheduledToast: (title, when) => `已安装定时任务「${title}」${when}`,
};

export function getBlueprintStrings(locale: string): BlueprintStrings {
  return locale === "zh" ? BLUEPRINT_STRINGS_ZH : BLUEPRINT_STRINGS_EN;
}

const BLUEPRINT_META_ZH: Record<
  string,
  { title: string; description: string }
> = {
  // 16 in-repo catalog blueprints (cron/blueprint_catalog.py)
  "morning-brief": {
    title: "早间简报",
    description: "每日精简早报：汇总今日日程、当地天气以及待处理的紧急事项。",
  },
  "important-mail": {
    title: "重要邮件监控",
    description: "定期检查收件箱，仅在收到真正需要关注的紧急邮件时提醒您。",
  },
  "weekly-review": {
    title: "每周复盘与规划",
    description: "每周总结：梳理本周已完成事项、未结项以及下周重点安排。",
  },
  "workday-start": {
    title: "工作日开工提醒",
    description: "每个工作日上午推送今日议程与最重要的优先事项。",
  },
  "custom-reminder": {
    title: "自定义定时提醒",
    description: "按您设定的时间与周期，发送自定义的提醒内容。",
  },
  "evening-winddown": {
    title: "晚间收尾小结",
    description: "每日晚间回顾：速览明日日程并提醒今晚需提前准备的事项。",
  },
  "news-digest": {
    title: "专题资讯摘要",
    description: "定期汇总您关注领域的最新资讯，自动去重，只推送全新动态。",
  },
  "bill-renewal-watch": {
    title: "账单与续费提醒",
    description: "在周期性账单、会员订阅自动扣费或到期前提前提醒，避免意外扣款。",
  },
  "price-watch": {
    title: "价格与库存监控",
    description: "监控指定商品、航班、酒店或房源，满足目标价格或有货条件时立即提醒。",
  },
  "competitor-watch": {
    title: "竞品动态监控",
    description: "追踪指定公司的重大新闻（新品发布、调价、融资、公告）并生成附带引用的摘要。",
  },
  "habit-checkin": {
    title: "习惯打卡提醒",
    description: "定期温和提醒您坚持目标习惯，并记录今日完成情况。",
  },
  "hydration-move": {
    title: "饮水与起身活动提醒",
    description: "工作日白天定时提醒您喝水、起身站立和拉伸放松。",
  },
  "meal-plan": {
    title: "每周食谱与采购清单",
    description: "根据您的饮食偏好与烹饪时间，生成未来一周的食谱及分类采购清单。",
  },
  "learn-daily": {
    title: "每日微课学习",
    description: "每天推送一节循序渐进的短篇知识微课，配有实例与思考题。",
  },
  "gratitude-journal": {
    title: "感恩与晚间反思",
    description: "每晚发送温和的反思引导，记录今日进展、值得感恩的事与小确幸。",
  },
  "on-this-day": {
    title: "历史上的今天与每日探索",
    description: "每天一条精选历史事件、科学冷知识、每日一词或名人名言。",
  },
  // Additional alias blueprint keys
  "daily-briefing": {
    title: "每日简报",
    description: "每日汇总今日日程、天气及待办重点。",
  },
  "inbox-triage": {
    title: "收件箱智能分拣",
    description: "自动巡检收件箱并筛选高优先级邮件。",
  },
  "standup-prep": {
    title: "站会准备助手",
    description: "工作日早晨自动汇总昨日进展与今日计划。",
  },
  "pr-review-digest": {
    title: "PR 评审摘要",
    description: "汇总待评审的 Pull Requests 及高风险变更。",
  },
  "ci-flake-watch": {
    title: "CI 稳定性巡检",
    description: "监控流水线偶发失败并定位不稳定测试。",
  },
  "dep-vuln-scan": {
    title: "依赖安全漏洞扫描",
    description: "定期检查项目依赖的安全公告与更新建议。",
  },
  "paper-digest": {
    title: "论文与研究摘要",
    description: "定期抓取关注领域的最新研究论文并生成精简摘要。",
  },
  "log-anomaly-scan": {
    title: "日志异常巡检",
    description: "定时扫描服务日志中的错误激增与异常堆栈。",
  },
  "disk-health-check": {
    title: "磁盘与空间健康检查",
    description: "检查设备磁盘占用、温度与健康状态。",
  },
  "backup-verify": {
    title: "备份完整性校验",
    description: "定期验证关键数据备份是否最新且可恢复。",
  },
  "weekly-retro": {
    title: "每周团队复盘",
    description: "汇总本周亮点、痛点与改进项。",
  },
  "goal-check-in": {
    title: "目标进度跟进",
    description: "定期检查阶段性目标完成进度与偏差。",
  },
  "reading-queue-nudge": {
    title: "稍后阅读提醒",
    description: "从阅读列表中挑选文章提醒您今日阅读。",
  },
  "hydration-stretch": {
    title: "饮水与拉伸提醒",
    description: "定时提醒喝水与活动肩颈。",
  },
  "end-of-day-shutdown": {
    title: "下班收尾清单",
    description: "工作结束前整理今日收尾事项并规划明日重点。",
  },
};

const BLUEPRINT_FIELD_LABEL_ZH: Record<string, string> = {
  "What time?": "执行时间",
  "Where to deliver?": "投递目标",
  "How often?": "执行频率",
  "Only notify me if the mail…": "仅在邮件满足以下条件时通知我…",
  "Which day?": "星期几？",
  "Remind me to…": "提醒内容…",
  "Repeat on": "重复周期",
  "What topic?": "关注主题",
  "How many bullets?": "摘要条数",
  "What's due?": "待付账单 / 续费项目",
  "What exactly to watch?": "监控目标",
  "Alert me when…": "触发提醒条件…",
  "Which companies?": "关注公司",
  "Which events matter?": "关注事件类型",
  "Which habit?": "打卡习惯",
  "Start hour": "开始时间（整点）",
  "End hour": "结束时间（整点）",
  "Diet?": "饮食偏好",
  "Meals per day?": "每日餐数",
  "Cooking effort?": "烹饪难度",
  "Learn about…": "学习主题…",
  "What kind?": "内容类型",
};

const BLUEPRINT_FIELD_HELP_ZH: Record<string, string> = {
  "24h local time, e.g. 08:00": "24 小时制本地时间，例如 08:00",
  "origin = the chat you set this up from (or your configured home channel when created from the dashboard); local = save only, no message; or any connected platform name":
    "origin = 发起配置的当前会话（在管理面板中创建时为已配置的主频道）；local = 仅保存在本地，不发送消息；或选择任意已连接的平台",
  "minutes between checks": "每次检查间隔的分钟数",
  "a subject, product, person, or search phrase": "主题、产品、人物或搜索关键词",
  "URL or precise description — variant, dates, seller":
    "商品链接或精确描述（型号、日期、商家）",
  "threshold price (state the currency), availability, or terms change":
    "目标价格（请注明币种）、有货状态或条款变更",
  "hours between checks — be gentle with rate limits":
    "每次检查间隔的小时数（避免触发频率限制）",
  "canonical names and domains; aliases help dedup":
    "公司标准名称与域名（填写别名有助于去重）",
  "hours between nudges": "每次提醒间隔的小时数",
  "first hour of the active window (24h)": "活动时段起始小时（24 小时制）",
  "last hour of the active window (24h)": "活动时段结束小时（24 小时制）",
};

const BLUEPRINT_TEXT_DEFAULT_ZH: Record<string, string> = {
  "needs a reply today, is from my manager or family, or mentions a deadline":
    "需要今天回复、来自上级或家人，或提及截止日期",
  "take a break and stretch": "休息一下，起身活动拉伸",
  "AI and technology": "人工智能与科技动态",
  "my streaming subscription renews soon": "流媒体会员即将自动续费",
  "a product URL or exact flight/hotel/listing description":
    "商品链接或航班/酒店/房源的精确描述",
  "the all-in price drops below my target": "总价低于我的目标价格",
  "two or three competitors, by canonical name":
    "两到三家竞争对手公司的标准名称",
  "product launches, pricing changes, funding, partnerships, executive moves, incidents":
    "产品发布、价格调整、融资动态、合作伙伴、高管变动、重大事件",
  "20 minutes of reading": "阅读 20 分钟",
  "Spanish vocabulary": "英语核心词汇",
};

const BLUEPRINT_OPTION_LABEL_ZH: Record<string, string> = {
  origin: "默认投递目标 (origin)",
  local: "仅保存在本地 (local)",
  telegram: "Telegram",
  discord: "Discord",
  email: "邮件 (Email)",
  everyday: "每天",
  weekdays: "工作日（周一至周五）",
  weekends: "周末（周六、周日）",
  sunday: "周日",
  monday: "周一",
  tuesday: "周二",
  wednesday: "周三",
  thursday: "周四",
  friday: "周五",
  saturday: "周六",
  "no restrictions": "无忌口",
  vegetarian: "素食",
  vegan: "纯素",
  "high-protein": "高蛋白",
  "low-carb": "低碳水",
  "dinner only": "仅晚餐",
  "lunch and dinner": "午餐和晚餐",
  "all three": "三餐（早/午/晚）",
  quick: "快手简餐",
  medium: "适中",
  ambitious: "精致大餐",
  "on this day in history": "历史上的今天",
  "word of the day": "每日一词",
  "science fact": "科学冷知识",
  "quote of the day": "每日名言",
};

const BLUEPRINT_TAG_ZH: Record<string, string> = {
  daily: "每日",
  briefing: "简报",
  email: "邮件",
  monitor: "监控",
  weekly: "每周",
  review: "复盘",
  focus: "专注",
  reminder: "提醒",
  evening: "晚间",
  digest: "摘要",
  research: "研究与资讯",
  finance: "账单与财务",
  prices: "价格",
  shopping: "购物",
  travel: "出行",
  competitors: "竞品",
  news: "资讯",
  habit: "习惯",
  wellbeing: "健康生活",
  food: "饮食",
  learning: "学习",
  reflection: "反思",
  curiosity: "探索",
  general: "通用",
  dev: "开发与运维",
  ops: "系统运维",
  productivity: "效率办公",
  wellness: "健康生活",
};

export function localizeBlueprintField(
  field: AutomationBlueprintField,
  locale: string,
): AutomationBlueprintField {
  if (locale !== "zh") return field;
  const label = BLUEPRINT_FIELD_LABEL_ZH[field.label] ?? field.label;
  const help = field.help
    ? (BLUEPRINT_FIELD_HELP_ZH[field.help] ?? field.help)
    : field.help;
  const def =
    field.type === "text" && field.default
      ? (BLUEPRINT_TEXT_DEFAULT_ZH[field.default] ?? field.default)
      : field.default;
  return { ...field, label, help, default: def };
}

export function localizeBlueprint(
  blueprint: AutomationBlueprint,
  locale: string,
): AutomationBlueprint {
  if (locale !== "zh") return blueprint;
  const meta = BLUEPRINT_META_ZH[blueprint.key];
  return {
    ...blueprint,
    title: meta?.title ?? blueprint.title,
    description: meta?.description ?? blueprint.description,
    fields: blueprint.fields.map((f) => localizeBlueprintField(f, locale)),
  };
}

export function localizeBlueprintOptionLabel(
  optionValue: string,
  locale: string,
): string {
  if (locale !== "zh") return optionValue;
  return BLUEPRINT_OPTION_LABEL_ZH[optionValue] ?? optionValue;
}

export function localizeBlueprintTag(tag: string, locale: string): string {
  if (locale !== "zh") return tag;
  return BLUEPRINT_TAG_ZH[tag] ?? tag;
}

/* ── Cron job badge & metadata localization ───────────────────────── */

const CRON_STATE_ZH: Record<string, string> = {
  scheduled: "已调度",
  enabled: "已启用",
  paused: "已暂停",
  disabled: "已禁用",
  error: "错误",
  completed: "已完成",
};

const CRON_LAST_STATUS_ZH: Record<string, string> = {
  ok: "成功",
  delivery_failed: "投递失败",
  blocked_config: "配置受阻",
  error: "执行错误",
};

const CRON_MODE_ZH: Record<string, string> = {
  no_agent: "纯脚本",
  "script+agent": "脚本+智能体",
  agent: "智能体",
};

export function localizeCronState(state: string, locale: string): string {
  if (locale !== "zh") return state;
  return CRON_STATE_ZH[state] ?? state;
}

export function localizeCronLastStatus(status: string, locale: string): string {
  if (locale !== "zh") return status;
  return CRON_LAST_STATUS_ZH[status] ?? status;
}

export function localizeCronMode(mode: string, locale: string): string {
  if (locale !== "zh") return mode;
  return CRON_MODE_ZH[mode] ?? mode;
}

export function localizeCronRepeat(job: CronJob, locale: string): string {
  const repeat = job.repeat;
  if (!repeat || repeat.times == null) {
    return locale === "zh" ? "永久" : "forever";
  }
  const completed = repeat.completed ?? 0;
  if (completed > 0) return `${completed}/${repeat.times}`;
  return locale === "zh" ? `${repeat.times} 次` : `${repeat.times} times`;
}

/* ── Channels catalog & field localization ────────────────────────── */

const CHANNEL_INFO_ZH: Record<string, { name: string; description: string }> = {
  weixin: {
    name: "微信（个人号）",
    description: "通过腾讯 iLink Bot API 接入个人微信号。",
  },
  wecom: {
    name: "企微智能机器人",
    description: "通过 Webhook 发送消息的企业微信群机器人。",
  },
  wecom_callback: {
    name: "企业微信自建应用",
    description: "通过回调应用实现企业微信双向对话。",
  },
  feishu: {
    name: "飞书 / Lark",
    description: "在飞书或 Lark 中使用 Hermes 智能体。",
  },
  dingtalk: {
    name: "钉钉",
    description: "将 Hermes 连接到钉钉群聊与私聊。",
  },
  qqbot: {
    name: "QQ 机器人",
    description: "将 Hermes 连接到 QQ 开放平台的 QQ 机器人。",
  },
  telegram: {
    name: "Telegram",
    description: "在 Telegram 私聊、群组与话题中运行 Hermes。",
  },
  discord: {
    name: "Discord",
    description: "将 Hermes 连接到 Discord 私信、频道与子区。",
  },
  slack: {
    name: "Slack",
    description:
      "通过 Socket Mode 在 Slack 中使用 Hermes，请添加允许交互的 Slack 成员 ID。",
  },
  mattermost: {
    name: "Mattermost",
    description: "将 Hermes 连接到 Mattermost 频道与私信。",
  },
  matrix: {
    name: "Matrix",
    description: "在 Matrix 房间与私信中使用 Hermes。",
  },
  whatsapp: {
    name: "WhatsApp",
    description: "通过内置 WhatsApp 桥接器扫码登录并使用 Hermes。",
  },
  whatsapp_cloud: {
    name: "WhatsApp Cloud API",
    description: "通过 Meta 官方托管的 WhatsApp Cloud API 使用 Hermes（无需本地桥接）。",
  },
  signal: {
    name: "Signal",
    description: "通过 signal-cli REST 桥接服务连接 Signal。",
  },
  bluebubbles: {
    name: "BlueBubbles (iMessage)",
    description: "通过 BlueBubbles 服务在 iMessage 中使用 Hermes。",
  },
  photon: {
    name: "iMessage (Photon)",
    description: "通过 Photon 托管的 Spectrum 平台在 iMessage 中使用 Hermes。",
  },
  email: {
    name: "电子邮件 (Email)",
    description: "通过 IMAP/SMTP 邮箱与 Hermes 对话。",
  },
  sms: {
    name: "短信 (Twilio)",
    description: "通过 Twilio 收发短信。",
  },
  google_chat: {
    name: "Google Chat",
    description: "通过 Cloud Pub/Sub 或 HTTP 事件将 Hermes 连接到 Google Chat。",
  },
  api_server: {
    name: "API 服务",
    description: "将 Hermes 暴露为兼容 OpenAI 的 HTTP API（可接入 Open WebUI 等前端）。",
  },
  webhook: {
    name: "Webhooks",
    description: "接收来自 GitHub、GitLab 等平台的 Webhook 事件。",
  },
  a2a: {
    name: "A2A (Agent-to-Agent)",
    description: "Agent-to-Agent 协议互联（仅使用标准库，无需额外安装包）。",
  },
  buzz: {
    name: "Buzz",
    description: "连接 Buzz 社区中继（需要在 PATH 或 BUZZ_CLI_PATH 中安装 buzz CLI）。",
  },
  irc: {
    name: "IRC",
    description: "在 IRC 频道或私信与 Hermes 之间中继消息。",
  },
  line: {
    name: "LINE",
    description: "通过 LINE Messaging API Webhook 在 LINE 中使用 Hermes。",
  },
  msgraph_webhook: {
    name: "Microsoft Graph Webhook",
    description: "接收 Microsoft Graph 变更通知（Teams 会议、Outlook 等）。",
  },
  teams: {
    name: "Microsoft Teams",
    description: "通过 Bot Framework 将 Hermes 连接到 Microsoft Teams 聊天。",
  },
  ntfy: {
    name: "ntfy 推送",
    description: "通过 ntfy 推送主题（ntfy.sh 或自建服务）与 Hermes 对话。",
  },
  raft: {
    name: "Raft",
    description: "作为外部智能体加入 Raft 工作区。",
  },
  relay: {
    name: "Relay（实验性）",
    description: "由 Hermes Relay 连接器前置的通用中继适配器。",
  },
  simplex: {
    name: "SimpleX Chat",
    description: "通过本地 simplex-chat 守护进程在 SimpleX Chat 中与 Hermes 对话。",
  },
  smartbox_voice: {
    name: "SmartBox 语音助手",
    description: "SmartBox 本地语音交互通道。",
  },
  homeassistant: {
    name: "Home Assistant",
    description: "连接 Home Assistant 智能家居事件与控制。",
  },
};

const CHANNEL_FIELD_ZH: Record<
  string,
  { prompt: string; description?: string; help?: string }
> = {
  // WeChat (weixin) — all 9 fields
  WEIXIN_ACCOUNT_ID: {
    prompt: "iLink 机器人账号 ID",
    description: "通过 hermes gateway setup 扫码登录获取的 iLink 机器人账号 ID",
  },
  WEIXIN_TOKEN: {
    prompt: "iLink 机器人令牌 (Token)",
    description: "通过 hermes gateway setup 扫码登录获取的 iLink 机器人令牌",
  },
  WEIXIN_BASE_URL: {
    prompt: "iLink API 基础地址",
    description:
      "扫码登录保存的 iLink API 基础地址（默认：https://ilinkai.weixin.qq.com）",
  },
  WEIXIN_ALLOWED_USERS: {
    prompt: "允许私聊的微信用户 ID（逗号分隔）",
    description: "允许与机器人私聊的微信用户 ID 白名单（逗号分隔）",
  },
  WEIXIN_CDN_BASE_URL: {
    prompt: "iLink 媒体 CDN 基础地址",
    description:
      "iLink 媒体文件 CDN 基础地址（默认：https://novac2c.cdn.weixin.qq.com/c2c）",
  },
  WEIXIN_DM_POLICY: {
    prompt: "私聊策略 (pairing|open|allowlist|disabled)",
    description:
      "私聊访问策略：pairing（配对码审批）、open（向所有人开放）、allowlist（仅白名单）或 disabled（禁用，默认：pairing）",
  },
  WEIXIN_GROUP_ALLOWED_USERS: {
    prompt: "允许的微信群聊 ID（逗号分隔）",
    description:
      "当群聊策略为 allowlist 时允许响应的微信群聊 ID 列表（逗号分隔）",
  },
  WEIXIN_GROUP_POLICY: {
    prompt: "群聊策略 (disabled|open|allowlist)",
    description:
      "群聊访问策略：disabled（禁用）、open（向所有群开放）或 allowlist（仅白名单，默认：disabled）",
  },
  WEIXIN_SPLIT_MULTILINE_MESSAGES: {
    prompt: "拆分多行消息？（true/false）",
    description: "将多行回复按行拆分为多条独立消息发送（默认：false）",
  },
  // WeCom & WeCom Callback
  WECOM_BOT_ID: {
    prompt: "企业微信机器人 ID",
    description: "企业微信智能群机器人 ID",
  },
  WECOM_SECRET: {
    prompt: "企业微信机器人密钥 (Secret)",
    description: "企业微信智能群机器人密钥",
  },
  WECOM_ALLOWED_USERS: {
    prompt: "允许的企业微信用户 ID（逗号分隔）",
    description: "允许与机器人对话的企业微信用户 ID 白名单（逗号分隔）",
  },
  WECOM_WEBSOCKET_URL: {
    prompt: "企业微信 WebSocket 地址",
    description: "企业微信智能机器人 WebSocket 连接地址",
  },
  WECOM_CALLBACK_CORP_ID: {
    prompt: "企业微信 Corp ID（企业 ID）",
    description: "企业微信回调模式企业 ID（自建应用）",
  },
  WECOM_CALLBACK_CORP_SECRET: {
    prompt: "企业微信应用 Secret",
    description: "企业微信回调模式自建应用密钥",
  },
  WECOM_CALLBACK_AGENT_ID: {
    prompt: "企业微信应用 Agent ID",
    description: "企业微信回调模式自建应用 Agent ID",
  },
  WECOM_CALLBACK_TOKEN: {
    prompt: "企业微信回调 Token",
    description: "企业微信接收消息回调校验 Token",
  },
  WECOM_CALLBACK_ENCODING_AES_KEY: {
    prompt: "企业微信回调 EncodingAESKey",
    description: "企业微信接收消息加解密 EncodingAESKey",
  },
  // Feishu / Lark
  FEISHU_APP_ID: {
    prompt: "飞书 / Lark App ID",
    description: "飞书或 Lark 开放平台应用的 App ID",
  },
  FEISHU_APP_SECRET: {
    prompt: "飞书 / Lark App Secret",
    description: "飞书或 Lark 开放平台应用的 App Secret",
  },
  FEISHU_ENCRYPT_KEY: {
    prompt: "事件订阅加密密钥 (Encrypt Key)",
    description: "飞书 / Lark 事件订阅加密 Key",
  },
  FEISHU_VERIFICATION_TOKEN: {
    prompt: "事件订阅校验令牌 (Verification Token)",
    description: "飞书 / Lark 事件订阅校验 Token",
  },
  FEISHU_ALLOWED_USERS: {
    prompt: "允许的飞书用户 ID（逗号分隔）",
    description: "允许与机器人对话的飞书用户 ID 列表（逗号分隔）",
  },
  FEISHU_DOMAIN: {
    prompt: "接入域名 (feishu/lark)",
    description: "服务区域：'feishu'（中国大陆）或 'lark'（国际版）",
  },
  // DingTalk
  DINGTALK_CLIENT_ID: {
    prompt: "钉钉 Client ID (AppKey)",
    description: "钉钉开放平台应用 AppKey (Client ID)",
  },
  DINGTALK_CLIENT_SECRET: {
    prompt: "钉钉 Client Secret (AppSecret)",
    description: "钉钉开放平台应用 AppSecret (Client Secret)",
  },
  DINGTALK_ALLOWED_USERS: {
    prompt: "允许的钉钉用户 ID（逗号分隔）",
    description: "允许与机器人对话的员工/发送者 ID（逗号分隔，* 表示允许所有人）",
  },
  DINGTALK_WEBHOOK_URL: {
    prompt: "钉钉自定义机器人 Webhook 地址（可选）",
    description: "用于跨平台或定时任务投递的钉钉群机器人 Webhook 地址",
  },
  // QQ Bot
  QQ_APP_ID: {
    prompt: "QQ 机器人 App ID",
    description: "来自 QQ 开放平台 (q.qq.com) 的机器人 App ID",
  },
  QQ_CLIENT_SECRET: {
    prompt: "QQ 机器人 Client Secret",
    description: "来自 QQ 开放平台的机器人 Client Secret",
  },
  QQ_ALLOWED_USERS: {
    prompt: "允许的 QQ 用户 ID",
    description: "允许使用机器人的 QQ 用户 ID 列表（逗号分隔）",
  },
  QQ_GROUP_ALLOWED_USERS: {
    prompt: "允许的 QQ 群 ID",
    description: "允许与机器人交互的 QQ 群 ID 列表（逗号分隔）",
  },
  QQ_MARKDOWN_SUPPORT: {
    prompt: "启用 QQ Markdown 消息 (true/false)",
    description: "在机器人具备权限时以 Markdown 格式发送 QQ 消息 (true/false)",
  },
  QQ_PORTAL_HOST: {
    prompt: "QQ 开放平台域名",
    description: "QQ 开放平台入口域名（默认 q.qq.com）",
  },
  QQ_SANDBOX: {
    prompt: "启用 QQ 沙箱模式 (true/false)",
    description: "开启开发测试用的 QQ 沙箱模式 (true/false)",
  },
  QQ_STT_API_KEY: {
    prompt: "QQ 语音转文字 API 密钥",
    description: "用于 QQ 语音消息转写的备用 STT API 密钥",
  },
  QQ_STT_BASE_URL: {
    prompt: "QQ 语音转文字 Base URL",
    description: "QQ 语音转写服务商的基础 URL",
  },
  QQ_STT_MODEL: {
    prompt: "QQ 语音转文字模型",
    description: "QQ 语音转写服务使用的模型名称",
  },
  // Telegram / Discord / Slack / WhatsApp / Email / SMS / API Server / Webhook
  TELEGRAM_BOT_TOKEN: {
    prompt: "Telegram 机器人令牌 (Bot Token)",
    description: "由 @BotFather 创建的完整机器人令牌（数字 ID + 冒号 + 密钥）",
  },
  TELEGRAM_ALLOWED_USERS: {
    prompt: "允许的 Telegram 用户数字 ID（逗号分隔）",
    description:
      "可选：立即允许访问的 Telegram 数字用户 ID（逗号分隔）；留空则通过私聊配对码审批新用户",
  },
  TELEGRAM_WEBHOOK_SECRET: {
    prompt: "Webhook 密钥",
    description:
      "Telegram 每次推送 Webhook 更新时附带的校验密钥（配合 TELEGRAM_WEBHOOK_URL 使用）",
  },
  DISCORD_BOT_TOKEN: {
    prompt: "Discord 机器人令牌 (Bot Token)",
    description: "从 Discord 开发者门户获取的 Bot Token",
  },
  DISCORD_ALLOWED_USERS: {
    prompt: "允许的 Discord 用户 ID（逗号分隔）",
    description: "允许使用机器人的 Discord 用户 ID 白名单（逗号分隔）",
  },
  SLACK_BOT_TOKEN: {
    prompt: "Slack Bot Token (xoxb-...)",
    description:
      "安装应用后从 OAuth & Permissions 获取的 Slack Bot Token (xoxb-)",
    help: "在 Slack 应用中添加所需 Bot 权限并安装到工作区，然后复制 OAuth & Permissions > Bot User OAuth Token。",
  },
  SLACK_APP_TOKEN: {
    prompt: "Slack App Token (xapp-...)",
    description:
      "用于 Socket Mode 的应用级令牌 (xapp-)，在 Basic Information → App-Level Tokens 中创建",
    help: "在 Slack 应用中启用 Socket Mode，然后在 Basic Information > App-Level Tokens 中创建包含 connections:write 权限的令牌。",
  },
  SLACK_ALLOWED_USERS: {
    prompt: "允许的 Slack 成员 ID",
    description:
      "允许使用 Hermes 的 Slack 成员 ID（逗号分隔，例如 U01ABC2DEF3）",
    help: "在 Slack 中打开个人资料，点击更多菜单并选择「复制成员 ID」，多个 ID 用逗号分隔。",
  },
  WHATSAPP_ENABLED: {
    prompt: "启用 WhatsApp？（true/false）",
    description: "启用 WhatsApp 适配器（需运行 Node.js 桥接服务）",
  },
  WHATSAPP_MODE: {
    prompt: "WhatsApp 模式",
    description: "WhatsApp 桥接运行模式（bot 或 self-chat）",
  },
  WHATSAPP_DM_POLICY: {
    prompt: "WhatsApp 私聊策略",
    description: "WhatsApp 私信授权策略",
  },
  WHATSAPP_ALLOWED_USERS: {
    prompt: "允许的 WhatsApp 用户（逗号分隔）",
    description: "允许与机器人对话的 WhatsApp 号码或用户 ID（逗号分隔）",
  },
  EMAIL_ADDRESS: {
    prompt: "邮箱地址",
    description: "用于收发邮件的电子邮箱地址",
  },
  EMAIL_PASSWORD: {
    prompt: "邮箱密码 / 应用专用密码",
    description: "邮箱账号密码或客户端应用专用密码",
  },
  EMAIL_IMAP_HOST: {
    prompt: "IMAP 服务器地址",
    description: "用于收取邮件的 IMAP 主机（例如 imap.gmail.com）",
  },
  EMAIL_SMTP_HOST: {
    prompt: "SMTP 服务器地址",
    description: "用于发送邮件的 SMTP 主机（例如 smtp.gmail.com）",
  },
  EMAIL_SMTP_PORT: {
    prompt: "SMTP 端口",
    description: "SMTP 服务器端口（默认 587）",
  },
  EMAIL_ALLOWED_USERS: {
    prompt: "允许的发件人邮箱（逗号分隔）",
    description: "允许与机器人通信的电子邮箱地址白名单（逗号分隔）",
  },
  EMAIL_AUTHSERV_ID: {
    prompt: "接收端 MTA authserv-id",
    description:
      "邮件服务器顶层 Authentication-Results 标头中的 authserv-id（如 mx.google.com）",
  },
  TWILIO_ACCOUNT_SID: {
    prompt: "Twilio Account SID",
    description: "Twilio 账户 SID",
  },
  TWILIO_AUTH_TOKEN: {
    prompt: "Twilio Auth Token",
    description: "Twilio 认证令牌",
  },
  TWILIO_PHONE_NUMBER: {
    prompt: "Twilio 短信号码",
    description: "支持短信功能的 Twilio 电话号码（E.164 格式）",
  },
  SMS_ALLOWED_USERS: {
    prompt: "允许的手机号码（逗号分隔）",
    description: "允许与机器人互发短信的电话号码列表（逗号分隔）",
  },
  API_SERVER_ENABLED: {
    prompt: "启用 API 服务 (true/false)",
    description:
      "启用兼容 OpenAI 的 HTTP API 服务（允许 Open WebUI、LobeChat 等前端接入）",
  },
  API_SERVER_KEY: {
    prompt: "API 服务认证密钥",
    description: "API 服务的 Bearer 认证密钥（启用 API 服务时必填）",
  },
  API_SERVER_PORT: {
    prompt: "API 服务端口",
    description: "API 服务监听端口（默认：8642）",
  },
  API_SERVER_HOST: {
    prompt: "API 服务绑定地址",
    description: "API 服务监听主机地址（默认：127.0.0.1）",
  },
  API_SERVER_MODEL_NAME: {
    prompt: "API 服务显示模型名称",
    description: "/v1/models 接口对外展示的模型名称（默认使用当前配置名）",
  },
  WEBHOOK_ENABLED: {
    prompt: "启用 Webhooks (true/false)",
    description: "启用 Webhook 平台适配器以接收来自 GitHub、GitLab 等的事件",
  },
  WEBHOOK_PORT: {
    prompt: "Webhook 服务端口",
    description: "Webhook HTTP 服务监听端口（默认：8644）",
  },
  WEBHOOK_SECRET: {
    prompt: "Webhook 签名密钥",
    description: "用于校验 Webhook 签名的全局 HMAC 密钥",
  },
};

const CHANNEL_STATE_LABEL_ZH: Record<string, string> = {
  connected: "已连接",
  pending_restart: "重启后生效",
  gateway_stopped: "网关已停止",
  startup_failed: "启动失败",
  disconnected: "已断开",
  not_configured: "待配置",
  disabled: "已禁用",
  fatal: "错误",
};

export function localizeChannelInfo(
  platform: Pick<MessagingPlatform, "id" | "name" | "description">,
  locale: string,
): { name: string; description: string } {
  if (locale !== "zh") {
    return { name: platform.name, description: platform.description };
  }
  const zhInfo = CHANNEL_INFO_ZH[platform.id];
  return {
    name: zhInfo?.name || platform.name,
    description: zhInfo?.description || platform.description,
  };
}

export function localizeChannelField(
  field: MessagingPlatformEnvVar,
  locale: string,
): { prompt: string; description: string; help: string } {
  const basePrompt = field.prompt || field.key;
  const baseDesc = field.description || "";
  const baseHelp = field.help || "";
  if (locale !== "zh") {
    return { prompt: basePrompt, description: baseDesc, help: baseHelp };
  }
  const zhField = CHANNEL_FIELD_ZH[field.key];
  return {
    prompt: zhField?.prompt || basePrompt,
    description: zhField?.description || baseDesc,
    help: zhField?.help || baseHelp,
  };
}

export function localizeChannelStateBadge(
  state: string,
  locale: string,
): string | undefined {
  if (locale !== "zh") return undefined;
  return CHANNEL_STATE_LABEL_ZH[state];
}
