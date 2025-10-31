import os
import subprocess
import sys

def run_process(name, command):
    print(f"Запускаем {name}…")
    try:
        subprocess.Popen([sys.executable, *command], creationflags=subprocess.CREATE_NEW_CONSOLE)
    except Exception as e:
        print(f"❌ Ошибка при запуске {name}: {e}")

def main():
    print("=== Liberty Country Launcher ===")
    print("Этот скрипт запускает бота, админ-панель и сайт проекта Liberty Country.\n")

    # Проверяем токен бота
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("⚠️ Переменная окружения DISCORD_TOKEN не задана. Бот может не запуститься.\n")

    # Пользователь выбирает, что запускать
    start_bot   = input("Запустить бота? (y/n): ").strip().lower() == "y"
    start_admin = input("Запустить админ-панель? (y/n): ").strip().lower() == "y"
    start_site  = input("Запустить основной сайт? (y/n): ").strip().lower() == "y"

    base_dir = os.path.dirname(os.path.abspath(__file__))

    if start_bot:
        bot_file = os.path.join(base_dir, "liberty_country_bot.py")
        if os.path.exists(bot_file):
            run_process("бота (liberty_country_bot.py)", [bot_file])
        else:
            print("❌ Файл liberty_country_bot.py не найден.")

    if start_admin:
        admin_file = os.path.join(base_dir, "lc_admin_app.py")
        alt_admin = os.path.join(base_dir, "lc_admin_app_auth.py")
        if os.path.exists(alt_admin):
            run_process("админ-панель (с авторизацией)", [alt_admin])
        elif os.path.exists(admin_file):
            run_process("админ-панель", [admin_file])
        else:
            print("❌ Файл админ-панели не найден.")

    if start_site:
        site_dir = os.path.join(base_dir, "lc_main_site")
        site_file = os.path.join(site_dir, "lc_main_site.py")
        if os.path.exists(site_file):
            os.chdir(site_dir)
            run_process("основной сайт", [site_file])
        else:
            print("❌ Основной сайт (lc_main_site.py) не найден.")

    print("\n✅ Все выбранные процессы запущены. Чтобы остановить — закрой консоли или нажми Ctrl+C.\n")

if __name__ == "__main__":
    main()
