/**
 * Code.gs — бэкенд для веб-формы регистрации (signup/index.html).
 * Разворачивается как Google Apps Script Web App — бесплатный "сервер без сервера",
 * не требует хостинга и обслуживания.
 *
 * Что делает при заполнении формы:
 *   1. Генерирует user_id и telegram_link_code.
 *   2. Создаёт личную Google Таблицу пользователя (копию шаблона реестра)
 *      и выдаёт на неё доступ по e-mail пользователя.
 *   3. Добавляет строку в общую таблицу "Users" (админскую, видите только вы).
 *   4. Возвращает пользователю ссылку на Telegram-бота (deep link с кодом
 *      привязки) и ссылку на его личную таблицу.
 *
 * Настройка (см. подробно signup/README.md):
 *   1. Создайте новую пустую Google Таблицу — это будет "мастер-таблица" (Users).
 *   2. Extensions → Apps Script → вставьте сюда этот код.
 *   3. В свойствах скрипта (Project Settings → Script Properties) добавьте:
 *        BOT_USERNAME = юзернейм вашего Telegram-бота без @
 *   4. Deploy → New deployment → Web app → Execute as: Me, Who has access: Anyone.
 *   5. Скопируйте URL деплоя в signup/index.html (APPS_SCRIPT_URL).
 */

const USERS_SHEET_NAME = "Users";
const REGISTRY_HEADERS = [
  "Дата", "Заголовок", "Компания", "Ссылка", "Источник",
  "Балл", "Вердикт", "Причина (ИИ)", "Статус",
  "Моя оценка", "Почему",
];
const USERS_HEADERS = [
  "user_id", "name", "email", "status",
  "candidate_summary", "target_titles", "seniority", "locations_allowed",
  "channels", "hh_companies", "hh_titles",
  "telegram_link_code", "telegram_chat_id",
  "personal_sheet_id", "state_json", "created_at",
];

function doPost(e) {
  try {
    const payload = JSON.parse(e.postData.contents);
    const result = registerUser(payload);
    return jsonResponse({ status: "ok", ...result });
  } catch (err) {
    return jsonResponse({ status: "error", message: String(err) });
  }
}

function jsonResponse(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function registerUser(payload) {
  // hh_titles больше не обязателен: если пусто, радар ищет по target_titles,
  // а каналы подбирает маршрутизация по базе источников.
  const requiredFields = ["name", "email", "candidate_summary", "target_titles", "seniority", "locations_allowed"];
  for (const f of requiredFields) {
    if (!payload[f]) throw new Error(`Не заполнено обязательное поле: ${f}`);
  }

  const usersSheet = getOrCreateUsersSheet();
  const userId = Utilities.getUuid();
  const linkCode = generateLinkCode();

  const personalSheet = createPersonalSheet(payload.name, payload.email);

  usersSheet.appendRow([
    userId,
    payload.name,
    payload.email,
    "active",
    payload.candidate_summary,
    payload.target_titles || "",
    payload.seniority || "",
    payload.locations_allowed || "",
    payload.channels || "",
    payload.hh_companies || "",
    payload.hh_titles || "",
    linkCode,
    "",                       // telegram_chat_id — заполнится после /start
    personalSheet.getId(),
    JSON.stringify({ shown_fingerprints: [] }),
    new Date().toISOString(),
  ]);

  const botUsername = PropertiesService.getScriptProperties().getProperty("BOT_USERNAME");
  if (!botUsername) throw new Error("Не задан BOT_USERNAME в Script Properties");

  return {
    telegram_deep_link: `https://t.me/${botUsername}?start=${linkCode}`,
    sheet_url: personalSheet.getUrl(),
  };
}

function getOrCreateUsersSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(USERS_SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(USERS_SHEET_NAME);
    sheet.appendRow(USERS_HEADERS);
  }
  return sheet;
}

function createPersonalSheet(userName, email) {
  const ss = SpreadsheetApp.create(`Радар вакансий — ${userName}`);
  const sheet = ss.getSheets()[0];
  sheet.setName("Реестр");
  sheet.appendRow(REGISTRY_HEADERS);
  sheet.setFrozenRows(1);

  // Доступ служебному аккаунту — без него multi_run.py не сможет писать сюда.
  const serviceEmail = PropertiesService.getScriptProperties().getProperty("SERVICE_ACCOUNT_EMAIL");
  if (serviceEmail) {
    try {
      ss.addEditor(serviceEmail);
    } catch (err) {
      Logger.log("Не удалось выдать доступ служебному аккаунту: " + err);
    }
  }

  if (email) {
    try {
      ss.addEditor(email);
    } catch (err) {
      // Иногда падает, если e-mail не Google-аккаунт — не фатально,
      // таблицу всё равно можно открыть по ссылке при доступе "у кого есть ссылка".
      ss.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.EDIT);
    }
  }
  return ss;
}

function generateLinkCode() {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"; // без похожих символов (0/O, 1/I)
  let code = "";
  for (let i = 0; i < 8; i++) {
    code += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return code;
}
