---
name: juegos-web-base
description: Arquitectura base para juegos simples de navegador con canvas
triggers: [juego web, juego en canvas, juego de navegador, phaser, juego de naves, arquitectura de un juego]
scope: [code, chat]
compact: |
  Un solo .html con canvas, sin librerías ni imágenes (formas dibujadas).
  Separa simulación (estado, colisiones, puntos) del dibujo. Teclas
  mapeadas a acciones en un solo lugar. Bucle con requestAnimationFrame y dt.
  Menú y HUD en el DOM. Estados: menú, jugando, game over.
---
## Reglas
1. Separa la simulación del dibujo: la simulación guarda entidades,
   colisiones, puntos y estado; el dibujo solo lo pinta.
2. Entrada: define acciones (mover, disparar, pausa) y mapea las teclas
   a ellas en un único lugar.
3. Bucle con requestAnimationFrame y dt; la velocidad no depende del equipo.
4. Todo en un solo .html, sin librerías ni imágenes externas: formas dibujadas.
5. Menú, puntuación y game over en el DOM; el canvas solo para el campo.
6. Guarda solo estado serializable, no objetos de dibujo.
7. No mezcles reglas del juego dentro del código de dibujo.
8. Cierra con una línea: cómo abrirlo y qué teclas usa.
