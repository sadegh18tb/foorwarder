import os
import asyncio
import requests
from bs4 import BeautifulSoup
from rubpy import Client
import time
import logging

# ==========================================
# تنظیمات سراسری (Global Configurations)
# ==========================================

# تنظیمات تلگرام
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHANNEL_ID = "@your_telegram_channel" # یا ID عددی

# تنظیمات روبیکا
# برای روبیکا در اجرای اول نیاز به دریافت کد تایید است تا نشست (Session) ساخته شود.
RUBIKA_SESSION_NAME = "rubika_session"
RUBIKA_CHANNEL_GUID = "YOUR_RUBIKA_CHANNEL_GUID" # شناسه‌ی GUID کانال روبیکا

# تنظیمات ایتا
EITAA_CHANNEL_ID = "your_eitaa_channel_id" # بدون @ (مثلا: varzesh3)

# تنظیمات عمومی
CHECK_INTERVAL_MINUTES = 5 # هر چند دقیقه کانال بررسی شود؟
LAST_POST_FILE = "last_post.txt"

# تنظیم لاگر برای چاپ پیام‌ها
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==========================================
# مدیریت وضعیت (State Management)
# ==========================================

class BotState:
    """کلاسی برای مدیریت آخرین پست پردازش شده جهت جلوگیری از ارسال تکراری"""

    @staticmethod
    def get_last_post_id():
        if os.path.exists(LAST_POST_FILE):
            with open(LAST_POST_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content.isdigit():
                    return int(content)
        return 0 # اگر فایلی نبود یا خالی بود

    @staticmethod
    def set_last_post_id(post_id):
        with open(LAST_POST_FILE, "w", encoding="utf-8") as f:
            f.write(str(post_id))


# ==========================================
# استخراج از ایتا (Web Scraper)
# ==========================================

class EitaaScraper:
    """کلاسی برای دریافت پیام‌ها از نسخه وب ایتا"""

    def __init__(self, channel_id):
        self.channel_url = f"https://eitaa.com/{channel_id}"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        }

    def get_new_messages(self, last_post_id):
        """لیست پیام‌های جدیدتر از last_post_id را برمی‌گرداند"""
        new_messages = []
        try:
            response = requests.get(self.channel_url, headers=self.headers, timeout=15)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, 'html.parser')
            # ساختار وب ایتا مشابه نسخه وب تلگرام است
            # معمولاً پیام‌ها در div با کلاس etme_widget_message یا مشابه قرار دارند
            # برای اطمینان پیام‌ها را بر اساس ویژگی data-post-id پیدا می‌کنیم

            messages = soup.find_all('div', class_=lambda x: x and 'message' in x.lower() and 'widget' in x.lower())

            for msg in messages:
                # استخراج ID پیام
                post_id_str = msg.get('data-post') or msg.get('data-message-id') or msg.get('id')
                if not post_id_str:
                    # تلاش برای یافتن لینک پیام که شامل id است
                    link = msg.find('a', href=lambda x: x and self.channel_url in x)
                    if link:
                        parts = link['href'].split('/')
                        if parts[-1].isdigit():
                            post_id_str = parts[-1]

                if not post_id_str:
                    continue

                try:
                    # فرمت معمول: channel/123
                    current_id = int(str(post_id_str).split('/')[-1])
                except ValueError:
                    continue

                # اگر پیام جدیدتر از آخرین پیام ثبت شده است
                if current_id > last_post_id:
                    text = self._extract_text(msg)
                    media_url = self._extract_media(msg)

                    new_messages.append({
                        'id': current_id,
                        'text': text,
                        'media_url': media_url
                    })

            # مرتب‌سازی پیام‌ها از قدیمی‌ترین به جدیدترین برای ارسال به ترتیب
            new_messages.sort(key=lambda x: x['id'])
            return new_messages

        except Exception as e:
            logger.error(f"Error fetching data from Eitaa: {e}")
            return []

    def _extract_text(self, msg_element):
        """استخراج متن پیام"""
        text_element = msg_element.find('div', class_=lambda x: x and 'text' in x.lower())
        if text_element:
            # تبدیل تگ‌های br به خط جدید
            for br in text_element.find_all("br"):
                br.replace_with("\n")
            return text_element.get_text(separator=" ").strip()
        return ""

    def _extract_media(self, msg_element):
        """استخراج لینک مدیا (عکس یا ویدیو) در صورت وجود"""
        # جستجو برای عکس
        photo_element = msg_element.find('a', class_=lambda x: x and 'photo' in x.lower())
        if photo_element and 'style' in photo_element.attrs:
            # لینک عکس معمولا در ویژگی style بک‌گراند است
            style = photo_element['style']
            if "background-image" in style:
                start = style.find("url('") + 5
                end = style.find("')", start)
                if start > 4 and end != -1:
                    return style[start:end]

        # جستجو برای ویدیو
        video_element = msg_element.find('video')
        if video_element and video_element.get('src'):
            return video_element['src']

        # جستجو برای تگ img مستقیم
        img_element = msg_element.find('img')
        if img_element and img_element.get('src'):
            return img_element['src']

        return None


# ==========================================
# ارسال به تلگرام
# ==========================================

class TelegramSender:
    """کلاسی برای ارسال پیام به تلگرام از طریق API رسمی"""

    def __init__(self, token, channel_id):
        self.token = token
        self.channel_id = channel_id
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def send_message(self, text, media_path=None):
        try:
            if media_path and os.path.exists(media_path):
                # ارسال فایل به همراه کپشن
                file_ext = os.path.splitext(media_path)[1].lower()
                is_video = file_ext in ['.mp4', '.avi', '.mkv']

                endpoint = "/sendVideo" if is_video else "/sendPhoto"
                url = self.base_url + endpoint

                with open(media_path, 'rb') as f:
                    files = {'video': f} if is_video else {'photo': f}
                    data = {'chat_id': self.channel_id, 'caption': text}
                    response = requests.post(url, data=data, files=files, timeout=30)
                    response.raise_for_status()
                    logger.info("Successfully sent media to Telegram.")
            else:
                # ارسال فقط متن
                if not text:
                    return # نه متن دارد نه مدیا

                url = self.base_url + "/sendMessage"
                data = {'chat_id': self.channel_id, 'text': text}
                response = requests.post(url, data=data, timeout=15)
                response.raise_for_status()
                logger.info("Successfully sent text to Telegram.")

            return True
        except Exception as e:
            logger.error(f"Error sending to Telegram: {e}")
            return False


# ==========================================
# ارسال به روبیکا
# ==========================================

class RubikaSender:
    """کلاسی برای ارسال پیام به روبیکا با استفاده از کتابخانه Rubpy"""

    def __init__(self, session_name, channel_guid):
        self.client = Client(session_name)
        self.channel_guid = channel_guid

    async def start(self):
        """شروع کلاینت و لاگین (در صورت نیاز کد تایید درخواست می‌شود)"""
        await self.client.start()

    async def send_message(self, text, media_path=None):
        try:
            if media_path and os.path.exists(media_path):
                # ارسال فایل (عکس یا ویدیو) به همراه کپشن
                await self.client.send_document(
                    self.channel_guid,
                    document=media_path,
                    caption=text
                )
                logger.info("Successfully sent media to Rubika.")
            else:
                # ارسال فقط متن
                if not text:
                    return
                await self.client.send_message(self.channel_guid, text)
                logger.info("Successfully sent text to Rubika.")
            return True
        except Exception as e:
            logger.error(f"Error sending to Rubika: {e}")
            return False


# ==========================================
# هماهنگ‌کننده اصلی (Auto Forwarder)
# ==========================================

class AutoForwarder:
    def __init__(self):
        self.eitaa = EitaaScraper(EITAA_CHANNEL_ID)
        self.telegram = TelegramSender(TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID)
        self.rubika = RubikaSender(RUBIKA_SESSION_NAME, RUBIKA_CHANNEL_GUID)

    def download_media(self, url):
        """دانلود موقت فایل مدیا"""
        if not url:
            return None

        try:
            # اصلاح آدرس نسبی به مطلق
            if url.startswith('/'):
                url = "https://eitaa.com" + url

            response = requests.get(url, stream=True, timeout=30)
            response.raise_for_status()

            # تشخیص پسوند فایل از URL یا Content-Type
            ext = ".jpg"
            if "video" in response.headers.get("Content-Type", "") or ".mp4" in url:
                ext = ".mp4"

            temp_file = f"temp_media{ext}"
            with open(temp_file, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            return temp_file
        except Exception as e:
            logger.error(f"Error downloading media from {url}: {e}")
            return None

    async def process_new_messages(self):
        """بررسی پیام‌های جدید و فوروارد آنها"""
        last_id = BotState.get_last_post_id()
        logger.info(f"Checking for new messages since ID: {last_id}")

        # ۱. دریافت پیام‌های جدید
        new_messages = self.eitaa.get_new_messages(last_id)

        if not new_messages:
            logger.info("No new messages found.")
            return

        logger.info(f"Found {len(new_messages)} new message(s).")

        for msg in new_messages:
            msg_id = msg['id']
            text = msg['text']
            media_url = msg['media_url']

            logger.info(f"Processing message ID: {msg_id}")

            # ۲. دانلود موقت مدیا در صورت وجود
            media_path = self.download_media(media_url) if media_url else None

            # ۳. ارسال به تلگرام
            # انجام در ترد جداگانه تا بلاک‌کننده نباشد
            tg_success = await asyncio.to_thread(self.telegram.send_message, text, media_path)

            # ۴. ارسال به روبیکا
            rb_success = await self.rubika.send_message(text, media_path)

            # ۵. پاک‌سازی فایل موقت
            if media_path and os.path.exists(media_path):
                try:
                    os.remove(media_path)
                except OSError as e:
                    logger.error(f"Error removing temp file {media_path}: {e}")

            # ۶. بروزرسانی State
            if tg_success or rb_success: # حتی اگر یکی هم موفق بود آی‌دی را ثبت می‌کنیم تا در حلقه گیر نیفتد
                BotState.set_last_post_id(msg_id)
                logger.info(f"State updated to post ID: {msg_id}")

            # مکث کوتاه بین ارسال پیام‌ها برای جلوگیری از اسپم شناخته شدن
            await asyncio.sleep(3)


async def main():
    logger.info("Starting AutoForwarder Bot...")
    forwarder = AutoForwarder()

    # لاگین به روبیکا (در اجرای اول ممکن است منتظر ورود کد باشد)
    logger.info("Connecting to Rubika...")
    await forwarder.rubika.start()
    logger.info("Rubika client started successfully.")

    # حلقه اصلی
    while True:
        try:
            await forwarder.process_new_messages()
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")

        logger.info(f"Waiting for {CHECK_INTERVAL_MINUTES} minutes before next check...")
        await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
