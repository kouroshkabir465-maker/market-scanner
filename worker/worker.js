// Scanner bot Telegram webhook -> instant ack + repository_dispatch (reply.yml). Separate from gold bot.
const OWNER = 8561650547;
const EXTRA = [];                      // other allowed chats (user's second account)
const CAPTURE_USER = "younesi1233";
const REPO = "kouroshkabir465-maker/market-scanner";
const KB = { keyboard: [[{ text: "🔍 بررسی کن" }]], resize_keyboard: true, is_persistent: true };
const isCheck = t => /^\/check\b|بررسی|^🔍/.test(t.trim());

export default {
  async fetch(req, env, ctx) {
    if (req.method !== "POST") return new Response("ok");
    if (req.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET)
      return new Response("forbidden", { status: 403 });
    let u; try { u = await req.json(); } catch { return new Response("bad", { status: 400 }); }
    const m = u.message || u.edited_message;
    if (!m || !m.chat) return new Response("ignored");
    const chat = m.chat.id;
    const tg = (method, body) => fetch(`https://api.telegram.org/bot${env.SCANNER_TELEGRAM_BOT_TOKEN}/${method}`,
      { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    if (chat !== OWNER && !EXTRA.includes(chat)) {
      if ((m.from?.username || "").toLowerCase() === CAPTURE_USER) ctx.waitUntil(Promise.all([
        fetch(`https://api.github.com/repos/${REPO}/dispatches`, { method: "POST",
          headers: { authorization: `Bearer ${env.GITHUB_TOKEN}`, accept: "application/vnd.github+json", "user-agent": "scanner-bot-worker", "content-type": "application/json" },
          body: JSON.stringify({ event_type: "chat_seen", client_payload: { bot: "scanner", chat, username: m.from.username } }) }),
        tg("sendMessage", { chat_id: chat, text: "✅ پیامت رسید؛ به‌زودی این حساب هم فعال میشه." })]));
      return new Response("ignored");
    }
    const text = (m.text || "").slice(0, 200);
    if (!isCheck(text)) {
      ctx.waitUntil(tg("sendMessage", { chat_id: chat, reply_markup: KB,
        text: "سلام داداش 👋 برای تحلیل لحظه‌ای یورو/دلار دکمهٔ «🔍 بررسی کن» رو بزن." }));
      return new Response("ok");
    }
    const ack = tg("sendMessage", { chat_id: chat, text: "در حال بررسی… ⏳", reply_markup: KB,
      reply_parameters: { message_id: m.message_id, allow_sending_without_reply: true } });
    const gh = fetch(`https://api.github.com/repos/${REPO}/dispatches`, {
      method: "POST",
      headers: { authorization: `Bearer ${env.GITHUB_TOKEN}`, accept: "application/vnd.github+json",
                 "user-agent": "scanner-bot-worker", "content-type": "application/json" },
      body: JSON.stringify({ event_type: "tg_check", client_payload: { chat, update_id: u.update_id } }),
    }).then(async r => {
      if (r.status >= 300) await tg("sendMessage", { chat_id: chat, text: "⚠️ بررسی شروع نشد، چند دقیقه دیگه دوباره امتحان کن." });
    });
    ctx.waitUntil(Promise.all([ack, gh]));
    return new Response("ok");
  },
};
