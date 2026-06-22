import sys
import os

def analyze_logs(file_path):
    stats = {
        'hunter_blocks': 0,
        'ob_blocks': 0,
        'rubber_band_blocks': 0,
        'trend_blocks': 0,
        'conflict_blocks': 0,
        'synergy_entries': 0,
        'reconnects': 0
    }

    if not os.path.exists(file_path):
        print(f"\n❌ Помилка: Файл '{file_path}' не знайдено.")
        print("Будь ласка, скопіюй логи з терміналу, створи файл logs.txt і встав їх туди.\n")
        return

    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            if "Мисливець не підтверджує збір стопів" in line:
                stats['hunter_blocks'] += 1
            elif "Стакан порожній" in line:
                stats['ob_blocks'] += 1
            elif "Rubber Band" in line:
                stats['rubber_band_blocks'] += 1
            elif "HTF Тренд-Фільтр" in line:
                stats['trend_blocks'] += 1
            elif "Конфлікт Тренду" in line:
                stats['conflict_blocks'] += 1
            elif "[СИНЕРГІЯ v12]" in line:
                stats['synergy_entries'] += 1
            elif "WebSocket реконнект успішний" in line:
                stats['reconnects'] += 1

    total_blocked = (stats['hunter_blocks'] + stats['ob_blocks'] + 
                     stats['rubber_band_blocks'] + stats['trend_blocks'] + 
                     stats['conflict_blocks'])
    total_signals = total_blocked + stats['synergy_entries']

    print("\n📊 АНАЛІЗ РОБОТИ ЗА ДЕНЬ (ВІДБИТІ ЗАГРОЗИ):")
    print("-" * 60)
    print(f"🎯 Снайпер знайшов потенційних точок входу: {total_signals}")
    print(f"✅ Угод відкрито (Синергія): {stats['synergy_entries']}")
    print("\n🛡️ Чому було заблоковано інші сигнали:")
    print(f" - Не було зняття ліквідності (Мисливець):  {stats['hunter_blocks']}")
    print(f" - Порожній стакан (Imbalance < 0.3):       {stats['ob_blocks']}")
    print(f" - Ціна за межами VWAP (Rubber Band):       {stats['rubber_band_blocks']}")
    print(f" - Конфлікт або фільтр 4H Тренду:           {stats['trend_blocks'] + stats['conflict_blocks']}")
    print("\n🔧 Технічна стабільність:")
    print(f" - Успішних реконнектів з біржею:           {stats['reconnects']}")
    print("-" * 60)
    
    if total_blocked > 0:
        print(f"💡 Висновок: Алгоритм врятував депозит від {total_blocked} потенційно збиткових угод.\n")
    else:
        print("💡 Висновок: Записів про заблоковані сигнали не знайдено.\n")

if __name__ == "__main__":
    analyze_logs('logs.txt')
