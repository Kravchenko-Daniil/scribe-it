# scribe — brand assets

Знак: буква **S** с вырезанной по центру звуковой волной (5 палочек).
Смысл: audio → text. Вайб: монохромный премиум, акцент Tiffany.

## Палитра
- near-black `#0E0E10` — фон
- Tiffany    `#0ABAB5` — основной акцент / знак
- cream      `#F2EDE4` — светлый знак на тёмном
- white      `#FFFFFF`

## Файлы
svg/  — векторные мастера (масштабируются без потерь)
  mark.svg          — знак, fill=currentColor (перекрашивается CSS)
  mark-tiffany.svg  — тиффани знак, прозрачный фон
  mark-cream.svg / mark-black.svg
  avatar.svg        — тиффани S на near-black, скруглённый квадрат (аватарка ТГ)
  avatar-circle.svg — превью под круглый кроп ТГ
  avatar-cream.svg

png/  — растровые экспорты
  avatar-1024.png / avatar-512.png — загрузка аватарки в Telegram
  mark-tiffany-512.png             — знак на прозрачном
  test-*                           — тесты мелкого размера

candidates/ — исходники генерации (Gemini/ChatGPT) + WINNER
refs/       — референсы (moodboard)
brief.md    — бриф
prompts.md  — промпты генерации

## Шрифт (для wordmark)
Space Grotesk (Google Fonts), Title/lowercase, letter-spacing -1..-2%.

## Don'ts
- не растягивать (только пропорционально)
- не менять цвета вне палитры
- не добавлять тени/градиенты/3D
- для favicon ≤16px — использовать упрощённую версию (волна сливается)
