# Liberty Country RP - Бот и веб-приложение

## Описание
Полноценная система для RP-сервера Liberty Country с Discord ботом и веб-интерфейсом.

## Компоненты проекта

### 1. Discord Бот (liberty_country_bot.py)
- Полная система управления гражданами
- Экономика, работа, бизнесы
- Транспорт и недвижимость
- Правоохранительные органы
- Медицинская система

### 2. Веб-приложение (web_app.py)
- Вход для пользователей и администраторов
- Личный кабинет
- Просмотр досье граждан
- Административная панель
- Управление экономикой сервера

## Установка

### Требования
- Python 3.8+
- SQLite3

### Шаги установки

1. Установите зависимости:
```bash
pip install -r requirements.txt
pip install py-cord sqlite3
```

2. Инициализация базы данных:
```python
# Запустите Python интерактивную оболочку
python

# Выполните:
from web_app import init_web_db
from liberty_country_bot import init_db, add_age_column_if_not_exists

init_db()
add_age_column_if_not_exists()
init_web_db()
```

3. Настройте бота:
- Откройте liberty_country_bot.py
- В самом конце файла найдите строку: bot.run('ВАШ_ТОКЕН')
- Замените 'ВАШ_ТОКЕН' на токен вашего Discord бота

4. Создайте первого администратора (в Python):
```python
from web_app import get_db
from werkzeug.security import generate_password_hash

conn = get_db()
c = conn.cursor()

# Замените данные на свои
c.execute("""
    INSERT INTO web_users (discord_id, username, password_hash, is_admin)
    VALUES (?, ?, ?, 1)
""", ('YOUR_DISCORD_ID', 'admin', generate_password_hash('admin123')))

conn.commit()
conn.close()
```

## Запуск

### Запуск Discord бота:
```bash
python liberty_country_bot.py
```

### Запуск веб-приложения:
```bash
python web_app.py
```

Веб-сайт будет доступен по адресу: http://localhost:5000

## Использование

### Для пользователей:
1. Зарегистрируйтесь на сайте с Discord ID
2. Войдите в личный кабинет
3. Просматривайте информацию о гражданах, бизнесах и недвижимости

### Для администраторов:
1. Войдите с правами администратора
2. Перейдите в админ-панель
3. Управляйте:
   - Гражданами
   - Деньгами
   - Розысками
   - Штрафами
   - Веб-пользователями

## Структура проекта
```
liberty_country/
├── liberty_country_bot.py     # Discord бот
├── web_app.py                  # Flask веб-приложение
├── requirements.txt            # Зависимости Python
├── README.md                   # Документация
├── liberty_country.db          # База данных SQLite
├── templates/                  # HTML шаблоны
│   ├── base.html
│   ├── index.html
│   ├── login.html
│   ├── register.html
│   ├── dashboard.html
│   ├── citizens.html
│   ├── citizen_profile.html
│   └── admin/
│       ├── dashboard.html
│       ├── citizens.html
│       └── money.html
└── static/                     # Статические файлы
    ├── css/
    │   └── style.css
    └── js/
        └── main.js
```

## Основные функции

### Discord команды:
- `!паспорт` - Получение/просмотр паспорта
- `!работать` - Заработок денег
- `!дома` - Список доступных домов
- `!бизнесы` - Список бизнесов
- `!mdt` - Полицейский терминал
- `!adminpanel` - Административная панель

### Веб-интерфейс:
- **Главная**: Статистика сервера
- **Личный кабинет**: Информация о персонаже
- **Граждане**: Список всех граждан
- **Бизнесы**: Активные бизнесы
- **Недвижимость**: Дома и аренда
- **Админ-панель**: Полное управление

## Безопасность

- Все пароли хэшируются с помощью Werkzeug
- Flask-Login для управления сессиями
- Защита административных маршрутов
- Валидация входных данных

## Техническая поддержка

При возникновении проблем:
1. Проверьте логи бота и веб-приложения
2. Убедитесь, что база данных инициализирована
3. Проверьте правильность Discord токена

## Лицензия
© 2025 Liberty Country RP. Все права защищены.
