"""
Liberty Country RP - Flask Web Application
Полноценный веб-сайт с входом для пользователей и администраторов
"""

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime, timezone
import sqlite3
import secrets
import os

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
app.config['SESSION_TYPE'] = 'filesystem'

# Flask-Login настройка
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# База данных
DATABASE = 'liberty_country.db'

def get_db():
    """Подключение к базе данных"""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

class User(UserMixin):
    """Модель пользователя для Flask-Login"""
    def __init__(self, id, discord_id, username, is_admin=False):
        self.id = id
        self.discord_id = discord_id
        self.username = username
        self.is_admin = is_admin

@login_manager.user_loader
def load_user(user_id):
    """Загрузка пользователя по ID"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM web_users WHERE id=?", (user_id,))
    user_data = c.fetchone()
    conn.close()

    if user_data:
        return User(
            id=user_data['id'],
            discord_id=user_data['discord_id'],
            username=user_data['username'],
            is_admin=bool(user_data['is_admin'])
        )
    return None

def admin_required(f):
    """Декоратор для проверки прав администратора"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('Требуются права администратора', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

def init_web_db():
    """Инициализация таблиц для веб-приложения"""
    conn = get_db()
    c = conn.cursor()

    # Таблица веб-пользователей
    c.execute("""
        CREATE TABLE IF NOT EXISTS web_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id TEXT UNIQUE,
            username TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            email TEXT,
            is_admin BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP
        )
    """)

    # Таблица сессий
    c.execute("""
        CREATE TABLE IF NOT EXISTS web_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            session_token TEXT,
            ip_address TEXT,
            user_agent TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES web_users (id)
        )
    """)

    conn.commit()
    conn.close()

@app.route('/')
def index():
    """Главная страница"""
    conn = get_db()
    c = conn.cursor()

    # Статистика сервера
    c.execute("SELECT COUNT(*) as total FROM citizens")
    total_citizens = c.fetchone()['total']

    c.execute("SELECT COUNT(*) as total FROM businesses")
    total_businesses = c.fetchone()['total']

    c.execute("SELECT COUNT(*) as total FROM houses WHERE status='owned'")
    owned_houses = c.fetchone()['total']

    conn.close()

    stats = {
        'total_citizens': total_citizens,
        'total_businesses': total_businesses,
        'owned_houses': owned_houses
    }

    return render_template('index.html', stats=stats)

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Вход в систему"""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM web_users WHERE username=?", (username,))
        user_data = c.fetchone()

        if user_data and check_password_hash(user_data['password_hash'], password):
            user = User(
                id=user_data['id'],
                discord_id=user_data['discord_id'],
                username=user_data['username'],
                is_admin=bool(user_data['is_admin'])
            )

            # Обновление времени последнего входа
            c.execute("UPDATE web_users SET last_login=? WHERE id=?", 
                     (datetime.now(timezone.utc), user_data['id']))

            # Логирование сессии
            c.execute("""
                INSERT INTO web_sessions (user_id, ip_address, user_agent)
                VALUES (?, ?, ?)
            """, (user_data['id'], request.remote_addr, request.user_agent.string))

            conn.commit()
            conn.close()

            login_user(user)
            flash('Успешный вход!', 'success')
            
            next_page = request.args.get('next')
            return redirect(next_page if next_page else url_for('dashboard'))
        else:
            flash('Неверное имя пользователя или пароль', 'danger')
            conn.close()

    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    """Регистрация нового пользователя"""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username')
        discord_id = request.form.get('discord_id')
        email = request.form.get('email')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        # Валидация
        if not all([username, discord_id, password, confirm_password]):
            flash('Все поля обязательны для заполнения', 'danger')
            return render_template('register.html')

        if password != confirm_password:
            flash('Пароли не совпадают', 'danger')
            return render_template('register.html')

        if len(password) < 6:
            flash('Пароль должен содержать минимум 6 символов', 'danger')
            return render_template('register.html')

        conn = get_db()
        c = conn.cursor()

        # Проверка существующего пользователя
        c.execute("SELECT * FROM web_users WHERE username=? OR discord_id=?", 
                 (username, discord_id))
        if c.fetchone():
            flash('Пользователь с таким именем или Discord ID уже существует', 'danger')
            conn.close()
            return render_template('register.html')

        # Проверка наличия гражданина в системе
        c.execute("SELECT * FROM citizens WHERE discord_id=?", (discord_id,))
        citizen = c.fetchone()
        if not citizen:
            flash('Discord ID не найден в базе граждан. Сначала зарегистрируйтесь на сервере.', 'danger')
            conn.close()
            return render_template('register.html')

        # Создание пользователя
        password_hash = generate_password_hash(password)
        c.execute("""
            INSERT INTO web_users (discord_id, username, password_hash, email)
            VALUES (?, ?, ?, ?)
        """, (discord_id, username, password_hash, email))

        conn.commit()
        conn.close()

        flash('Регистрация успешна! Войдите в систему.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    """Выход из системы"""
    logout_user()
    flash('Вы вышли из системы', 'info')
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    """Личный кабинет пользователя"""
    conn = get_db()
    c = conn.cursor()

    # Получение данных гражданина
    c.execute("SELECT * FROM citizens WHERE discord_id=?", (current_user.discord_id,))
    citizen = c.fetchone()

    # Получение транспорта
    c.execute("SELECT * FROM vehicles WHERE owner_id=?", (citizen['id'],))
    vehicles = c.fetchall()

    # Получение бизнесов
    c.execute("SELECT * FROM businesses WHERE owner_id=?", (citizen['id'],))
    businesses = c.fetchall()

    # Получение дома
    c.execute("SELECT * FROM houses WHERE owner_id=? OR renter_id=?", 
             (citizen['id'], citizen['id']))
    house = c.fetchone()

    # Получение штрафов
    c.execute("SELECT * FROM fines WHERE citizen_id=? AND paid=0", (citizen['id'],))
    fines = c.fetchall()

    # Получение ордеров на розыск
    c.execute("SELECT * FROM warrants WHERE citizen_id=? AND status='active'", 
             (citizen['id'],))
    warrants = c.fetchall()

    conn.close()

    return render_template('dashboard.html', 
                         citizen=citizen,
                         vehicles=vehicles,
                         businesses=businesses,
                         house=house,
                         fines=fines,
                         warrants=warrants)

@app.route('/citizens')
@login_required
def citizens_list():
    """Список всех граждан"""
    conn = get_db()
    c = conn.cursor()

    page = request.args.get('page', 1, type=int)
    per_page = 20
    offset = (page - 1) * per_page

    # Поиск
    search = request.args.get('search', '')
    if search:
        c.execute("""
            SELECT * FROM citizens 
            WHERE first_name LIKE ? OR last_name LIKE ? OR discord_id LIKE ?
            LIMIT ? OFFSET ?
        """, (f'%{search}%', f'%{search}%', f'%{search}%', per_page, offset))
    else:
        c.execute("SELECT * FROM citizens LIMIT ? OFFSET ?", (per_page, offset))

    citizens = c.fetchall()

    # Подсчет общего количества
    c.execute("SELECT COUNT(*) as total FROM citizens")
    total = c.fetchone()['total']

    conn.close()

    total_pages = (total + per_page - 1) // per_page

    return render_template('citizens.html', 
                         citizens=citizens,
                         page=page,
                         total_pages=total_pages,
                         search=search)

@app.route('/citizen/<int:citizen_id>')
@login_required
def citizen_profile(citizen_id):
    """Профиль гражданина"""
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT * FROM citizens WHERE id=?", (citizen_id,))
    citizen = c.fetchone()

    if not citizen:
        flash('Гражданин не найден', 'danger')
        return redirect(url_for('citizens_list'))

    # Дополнительные данные
    c.execute("SELECT * FROM vehicles WHERE owner_id=?", (citizen_id,))
    vehicles = c.fetchall()

    c.execute("SELECT * FROM businesses WHERE owner_id=?", (citizen_id,))
    businesses = c.fetchall()

    c.execute("SELECT * FROM houses WHERE owner_id=? OR renter_id=?", 
             (citizen_id, citizen_id))
    house = c.fetchone()

    c.execute("SELECT * FROM criminal_records WHERE citizen_id=?", (citizen_id,))
    criminal_records = c.fetchall()

    c.execute("SELECT * FROM warrants WHERE citizen_id=?", (citizen_id,))
    warrants = c.fetchall()

    conn.close()

    return render_template('citizen_profile.html',
                         citizen=citizen,
                         vehicles=vehicles,
                         businesses=businesses,
                         house=house,
                         criminal_records=criminal_records,
                         warrants=warrants)

@app.route('/businesses')
@login_required
def businesses_list():
    """Список всех бизнесов"""
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        SELECT b.*, c.first_name, c.last_name, c.discord_id
        FROM businesses b
        LEFT JOIN citizens c ON b.owner_id = c.id
    """)
    businesses = c.fetchall()

    conn.close()

    return render_template('businesses.html', businesses=businesses)

@app.route('/houses')
@login_required
def houses_list():
    """Список всех домов"""
    conn = get_db()
    c = conn.cursor()

    district = request.args.get('district', '')

    if district:
        c.execute("""
            SELECT h.*, 
                   o.first_name as owner_first_name, o.last_name as owner_last_name,
                   r.first_name as renter_first_name, r.last_name as renter_last_name
            FROM houses h
            LEFT JOIN citizens o ON h.owner_id = o.id
            LEFT JOIN citizens r ON h.renter_id = r.id
            WHERE h.district = ?
        """, (district,))
    else:
        c.execute("""
            SELECT h.*, 
                   o.first_name as owner_first_name, o.last_name as owner_last_name,
                   r.first_name as renter_first_name, r.last_name as renter_last_name
            FROM houses h
            LEFT JOIN citizens o ON h.owner_id = o.id
            LEFT JOIN citizens r ON h.renter_id = r.id
        """)

    houses = c.fetchall()

    # Получение списка районов
    c.execute("SELECT DISTINCT district FROM houses")
    districts = [row['district'] for row in c.fetchall()]

    conn.close()

    return render_template('houses.html', houses=houses, districts=districts, 
                         selected_district=district)

@app.route('/vehicles')
@login_required
def vehicles_list():
    """Список всех транспортных средств"""
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        SELECT v.*, c.first_name, c.last_name, c.discord_id
        FROM vehicles v
        LEFT JOIN citizens c ON v.owner_id = c.id
    """)
    vehicles = c.fetchall()

    conn.close()

    return render_template('vehicles.html', vehicles=vehicles)

# ===== АДМИНИСТРАТИВНАЯ ПАНЕЛЬ =====

@app.route('/admin')
@login_required
@admin_required
def admin_panel():
    """Главная страница админ-панели"""
    conn = get_db()
    c = conn.cursor()

    # Статистика
    c.execute("SELECT COUNT(*) as total FROM citizens")
    total_citizens = c.fetchone()['total']

    c.execute("SELECT COUNT(*) as total FROM businesses")
    total_businesses = c.fetchone()['total']

    c.execute("SELECT COUNT(*) as total FROM warrants WHERE status='active'")
    active_warrants = c.fetchone()['total']

    c.execute("SELECT COUNT(*) as total FROM fines WHERE paid=0")
    unpaid_fines = c.fetchone()['total']

    c.execute("SELECT SUM(bank) as total FROM citizens")
    total_money = c.fetchone()['total'] or 0

    # Последние действия
    c.execute("""
        SELECT * FROM web_sessions 
        ORDER BY created_at DESC 
        LIMIT 10
    """)
    recent_sessions = c.fetchall()

    conn.close()

    stats = {
        'total_citizens': total_citizens,
        'total_businesses': total_businesses,
        'active_warrants': active_warrants,
        'unpaid_fines': unpaid_fines,
        'total_money': total_money
    }

    return render_template('admin/dashboard.html', 
                         stats=stats,
                         recent_sessions=recent_sessions)

@app.route('/admin/citizens')
@login_required
@admin_required
def admin_citizens():
    """Управление гражданами"""
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT * FROM citizens ORDER BY id DESC")
    citizens = c.fetchall()

    conn.close()

    return render_template('admin/citizens.html', citizens=citizens)

@app.route('/admin/citizen/<int:citizen_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_edit_citizen(citizen_id):
    """Редактирование гражданина"""
    conn = get_db()
    c = conn.cursor()

    if request.method == 'POST':
        first_name = request.form.get('first_name')
        last_name = request.form.get('last_name')
        cash = request.form.get('cash', type=int)
        bank = request.form.get('bank', type=int)
        job = request.form.get('job')

        c.execute("""
            UPDATE citizens 
            SET first_name=?, last_name=?, cash=?, bank=?, job=?
            WHERE id=?
        """, (first_name, last_name, cash, bank, job, citizen_id))

        conn.commit()
        conn.close()

        flash('Данные гражданина обновлены', 'success')
        return redirect(url_for('admin_citizens'))

    c.execute("SELECT * FROM citizens WHERE id=?", (citizen_id,))
    citizen = c.fetchone()
    conn.close()

    if not citizen:
        flash('Гражданин не найден', 'danger')
        return redirect(url_for('admin_citizens'))

    return render_template('admin/edit_citizen.html', citizen=citizen)

@app.route('/admin/citizen/<int:citizen_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_delete_citizen(citizen_id):
    """Удаление гражданина"""
    conn = get_db()
    c = conn.cursor()

    # Удаление всех связанных данных
    c.execute("DELETE FROM vehicles WHERE owner_id=?", (citizen_id,))
    c.execute("DELETE FROM businesses WHERE owner_id=?", (citizen_id,))
    c.execute("DELETE FROM warrants WHERE citizen_id=?", (citizen_id,))
    c.execute("DELETE FROM fines WHERE citizen_id=?", (citizen_id,))
    c.execute("DELETE FROM criminal_records WHERE citizen_id=?", (citizen_id,))
    c.execute("DELETE FROM citizens WHERE id=?", (citizen_id,))

    conn.commit()
    conn.close()

    flash('Гражданин удален', 'success')
    return redirect(url_for('admin_citizens'))

@app.route('/admin/money', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_money():
    """Управление деньгами"""
    if request.method == 'POST':
        discord_id = request.form.get('discord_id')
        amount = request.form.get('amount', type=int)
        action = request.form.get('action')  # add/remove
        currency = request.form.get('currency')  # cash/bank

        conn = get_db()
        c = conn.cursor()

        c.execute("SELECT * FROM citizens WHERE discord_id=?", (discord_id,))
        citizen = c.fetchone()

        if not citizen:
            flash('Гражданин не найден', 'danger')
            conn.close()
            return redirect(url_for('admin_money'))

        if action == 'add':
            if currency == 'cash':
                c.execute("UPDATE citizens SET cash = cash + ? WHERE id=?", 
                         (amount, citizen['id']))
            else:
                c.execute("UPDATE citizens SET bank = bank + ? WHERE id=?", 
                         (amount, citizen['id']))
            flash(f'Добавлено {amount}$ в {currency}', 'success')
        else:
            if currency == 'cash':
                c.execute("UPDATE citizens SET cash = cash - ? WHERE id=?", 
                         (amount, citizen['id']))
            else:
                c.execute("UPDATE citizens SET bank = bank - ? WHERE id=?", 
                         (amount, citizen['id']))
            flash(f'Снято {amount}$ из {currency}', 'success')

        conn.commit()
        conn.close()

    return render_template('admin/money.html')

@app.route('/admin/warrants')
@login_required
@admin_required
def admin_warrants():
    """Управление розыском"""
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        SELECT w.*, c.first_name, c.last_name, c.discord_id
        FROM warrants w
        LEFT JOIN citizens c ON w.citizen_id = c.id
        ORDER BY w.issue_date DESC
    """)
    warrants = c.fetchall()

    conn.close()

    return render_template('admin/warrants.html', warrants=warrants)

@app.route('/admin/fines')
@login_required
@admin_required
def admin_fines():
    """Управление штрафами"""
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        SELECT f.*, c.first_name, c.last_name, c.discord_id
        FROM fines f
        LEFT JOIN citizens c ON f.citizen_id = c.id
        ORDER BY f.issue_date DESC
    """)
    fines = c.fetchall()

    conn.close()

    return render_template('admin/fines.html', fines=fines)

@app.route('/admin/users')
@login_required
@admin_required
def admin_users():
    """Управление веб-пользователями"""
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT * FROM web_users ORDER BY created_at DESC")
    users = c.fetchall()

    conn.close()

    return render_template('admin/users.html', users=users)

@app.route('/admin/user/<int:user_id>/toggle_admin', methods=['POST'])
@login_required
@admin_required
def admin_toggle_admin(user_id):
    """Переключение прав администратора"""
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT is_admin FROM web_users WHERE id=?", (user_id,))
    user = c.fetchone()

    if user:
        new_status = not bool(user['is_admin'])
        c.execute("UPDATE web_users SET is_admin=? WHERE id=?", 
                 (new_status, user_id))
        conn.commit()
        flash(f"Права администратора {'выданы' if new_status else 'отозваны'}", 'success')

    conn.close()
    return redirect(url_for('admin_users'))

# API endpoints
@app.route('/api/stats')
@login_required
def api_stats():
    """API для получения статистики"""
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT COUNT(*) as total FROM citizens")
    total_citizens = c.fetchone()['total']

    c.execute("SELECT COUNT(*) as total FROM businesses")
    total_businesses = c.fetchone()['total']

    c.execute("SELECT COUNT(*) as total FROM vehicles")
    total_vehicles = c.fetchone()['total']

    conn.close()

    return jsonify({
        'total_citizens': total_citizens,
        'total_businesses': total_businesses,
        'total_vehicles': total_vehicles
    })

# Инициализация при запуске
if __name__ == '__main__':
    init_web_db()
    app.run(debug=True, host='0.0.0.0', port=5000)
