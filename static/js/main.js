// Liberty Country RP - Main JavaScript

document.addEventListener('DOMContentLoaded', function() {
    // Автоматическое скрытие алертов
    const alerts = document.querySelectorAll('.alert');
    alerts.forEach(alert => {
        setTimeout(() => {
            const bsAlert = new bootstrap.Alert(alert);
            bsAlert.close();
        }, 5000);
    });

    // Подтверждение удаления
    const deleteButtons = document.querySelectorAll('[data-confirm]');
    deleteButtons.forEach(button => {
        button.addEventListener('click', function(e) {
            if (!confirm(this.dataset.confirm)) {
                e.preventDefault();
            }
        });
    });

    // Динамическое обновление статистики (если на главной странице)
    if (document.getElementById('stats-section')) {
        updateStats();
        setInterval(updateStats, 60000); // Обновление каждую минуту
    }
});

// Функция обновления статистики
async function updateStats() {
    try {
        const response = await fetch('/api/stats');
        const data = await response.json();

        // Обновление значений на странице
        if (document.getElementById('total-citizens')) {
            document.getElementById('total-citizens').textContent = data.total_citizens;
        }
        if (document.getElementById('total-businesses')) {
            document.getElementById('total-businesses').textContent = data.total_businesses;
        }
        if (document.getElementById('total-vehicles')) {
            document.getElementById('total-vehicles').textContent = data.total_vehicles;
        }
    } catch (error) {
        console.error('Ошибка обновления статистики:', error);
    }
}

// Функция форматирования чисел
function formatNumber(num) {
    return new Intl.NumberFormat('ru-RU').format(num);
}

// Функция для поиска в таблицах
function searchTable(inputId, tableId) {
    const input = document.getElementById(inputId);
    const filter = input.value.toUpperCase();
    const table = document.getElementById(tableId);
    const tr = table.getElementsByTagName('tr');

    for (let i = 1; i < tr.length; i++) {
        let txtValue = tr[i].textContent || tr[i].innerText;
        if (txtValue.toUpperCase().indexOf(filter) > -1) {
            tr[i].style.display = '';
        } else {
            tr[i].style.display = 'none';
        }
    }
}

// Копирование в буфер обмена
function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(() => {
        showNotification('Скопировано в буфер обмена!', 'success');
    }).catch(err => {
        console.error('Ошибка копирования:', err);
    });
}

// Показ уведомлений
function showNotification(message, type = 'info') {
    const alertDiv = document.createElement('div');
    alertDiv.className = `alert alert-${type} alert-dismissible fade show`;
    alertDiv.role = 'alert';
    alertDiv.innerHTML = `
        ${message}
        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
    `;

    const container = document.querySelector('.container');
    container.insertBefore(alertDiv, container.firstChild);

    setTimeout(() => {
        const bsAlert = new bootstrap.Alert(alertDiv);
        bsAlert.close();
    }, 5000);
}
