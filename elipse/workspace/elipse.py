import os
import time
import random
import msvcrt
import sys

# Configuración del juego
WIDTH = 40
HEIGHT = 20
BIRD_CHAR = "O"
PIPE_CHAR = "#"
GAP_SIZE = 5


def clear_screen():
    # Limpia la terminal de forma rápida
    os.system('cls' if os.name == 'nt' else 'clear')


def draw_game(bird_y, pipes, score):
    display = [[" " for _ in range(WIDTH)] for _ in range(HEIGHT)]
    
    # Dibujar tuberías
    for pipe_x, gap_y in pipes:
        if 0 <= pipe_x < WIDTH:
            for y in range(HEIGHT):
                if y < gap_y or y >= gap_y + GAP_SIZE:
                    display[y][pipe_x] = PIPE_CHAR

    # Dibujar pájaro
    bird_y_int = int(bird_y)
    if 0 <= bird_y_int < HEIGHT:
        display[bird_y_int][5] = BIRD_CHAR

    # Renderizar todo de un golpe
    output = f"Score: {score} | Presiona 'Espacio' para saltar | 'q' para salir\n"
    output += "+" + "-" * WIDTH + "+\n"
    for row in display:
        output += "|" + "".join(row) + "|\n"
    output += "+" + "-" * WIDTH + "+"
    print(output)


def main():
    bird_y = HEIGHT // 2
    velocity = 0
    gravity = 0.6
    jump_strength = -1.5
    score = 0
    
    # pipes: lista de [x, gap_y]
    pipes = [[WIDTH, random.randint(2, HEIGHT - GAP_SIZE - 2)]]
    
    print("¡Listo! Presiona cualquier tecla para empezar...")
    msvcrt.getch()

    while True:
        # 1. Entrada de teclado (sin bloquear el juego)
        if msvcrt.kbhit():
            key = msvcrt.getch()
            if key == b' ':
                velocity = jump_strength
            elif key == b'q':
                break

        # 2. Física
        velocity += gravity
        bird_y += velocity
        
        # Mover tuberías
        for pipe in pipes:
            pipe[0] -= 1
            
        # Generar nuevas tuberías
        if pipes[-1][0] < WIDTH - 15:
            pipes.append([WIDTH, random.randint(2, HEIGHT - GAP_SIZE - 2)])
            
        # Eliminar tuberías viejas y sumar puntos
        if pipes[0][0] < 0:
            pipes.pop(0)
            score += 1

        # 3. Colisiones
        bird_y_int = int(bird_y)
        # Colisión con suelo/techo
        if bird_y_int < 0 or bird_y_int >= HEIGHT:
            break
            
        # Colisión con tuberías
        for pipe_x, gap_y in pipes:
            if pipe_x == 5: # Posición X del pájaro
                if bird_y_int < gap_y or bird_y_int >= gap_y + GAP_SIZE:
                    print(f"\n¡GAME OVER! Puntaje final: {score}")
                    return

        # 4. Dibujar
        clear_screen()
        draw_game(bird_y, pipes, score)
        
        time.sleep(0.05) # Control de velocidad

    print(f"\n¡Juego terminado! Puntaje final: {score}")

if __name__ == "__main__":
    main()
