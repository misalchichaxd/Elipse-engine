---
name: code-review
description: Revisa código con foco en claridad, bugs y seguridad básica
triggers: [revisá, revisa este, code review, revisar código, revisar codigo, qué opinás de este código]
priority: 60
compact: |
  Revisá en este orden: 1) bugs reales, 2) seguridad básica, 3) claridad. Máximo 5 observaciones,
  cada una con la línea afectada y la corrección propuesta. Si está bien, decilo sin inventar problemas.
---
# Revisión de código

Cuando esta skill esté activa:

1. Leé el código completo antes de opinar. Si viene como archivo adjunto, usá `read_file`.
2. Ordená los hallazgos por gravedad: bugs que rompen algo, seguridad (inyección, secretos en el
   código, entradas sin validar), después claridad y nombres.
3. Para cada hallazgo: qué pasa, por qué importa y cómo se corrige (con el fragmento corregido).
4. No reescribas todo el archivo: señalá solo lo que cambia.
5. Cerrá con una línea de veredicto: "listo", "listo con cambios menores" o "necesita cambios".
