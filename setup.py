#!/usr/bin/env python3
"""
Liberty Country RP - Скрипт быстрого запуска
Автоматически инициализирует базу данных и создает первого администратора
"""

import sqlite3
import os
from werkzeug.security import generate_password_hash

def init_database():
    """Инициализация базы данных"""
    print("🔧 Инициализация базы данных...")

    from liberty_country_bot import init_db, add_age_column_if_not_exists
    from web_app import init_web_db

    init_db()
    add_age_column_if_not_exists()
    init_web_db()

    print("✅ База данных инициализирована")

def create_admin_user():
    """Создание первого администратора"""
    print("\n👑 Создание администратора...")

    discord_id = input("Введите Discord ID администратора: ")
    username = input("Введите имя пользователя (по умолчанию 'admin'): ") or "admin"
    password = input("Введите пароль (по умолчанию 'admin123'): ") or "admin123"

    conn = sqlite3.connect('liberty_country.db')
    c = conn.cursor()

    # Проверка существования пользователя
    c.execute("SELECT * FROM web_users WHERE discord_id=?", (discord_id,))
    if c.fetchone():
        print("⚠️ Пользователь с таким Discord ID уже существует")
        conn.close()
        return

    password_hash = generate_password_hash(password)

    c.execute("""
        INSERT INTO web_users (discord_id, username, password_hash, is_admin)
        VALUES (?, ?, ?, 1)
    """, (discord_id, username, password_hash))

    conn.commit()
    conn.close()

    print(f"✅ Администратор {username} создан успешно!")
    print(f"   Discord ID: {discord_id}")
    print(f"   Пароль: {password}")

def main():
    """Главная функция"""
    print("=" * 60)
    print("   LIBERTY COUNTRY RP - Система инициализации")
    print("=" * 60)

    # Проверка существования БД
    if os.path.exists('liberty_country.db'):
        response = input("\n⚠️ База данных уже существует. Переинициализировать? (y/n): ")
        if response.lower() != 'y':
            print("Отмена инициализации.")
            return

    # Инициализация БД
    init_database()

    # Создание администратора
    response = input("\nСоздать администратора? (y/n): ")
    if response.lower() == 'y':
        create_admin_user()

    print("\n" + "=" * 60)
    print("✅ Инициализация завершена!")
    print("=" * 60)
    print("\nДля запуска:")
    print("  Discord бот: python liberty_country_bot.py")
    print("  Веб-сайт:    python web_app.py")
    print("\nВеб-сайт будет доступен по адресу: http://localhost:5000")

if __name__ == '__main__':
    main()
