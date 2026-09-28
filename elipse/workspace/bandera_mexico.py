import turtle

def dibujar_bandera():
    t = turtle.Turtle()
    t.speed(3)
    
    # Configuración inicial
    pantalla = turtle.Screen()
    pantalla.setup(800, 500)
    t.penup()
    t.goto(-300, 150)
    t.pendown()

    # Tamaño de cada franja (ancho total 600, alto 300)
    ancho = 200
    alto = 300

    # Franja Verde
    t.color("green")
    t.begin_fill()
    for _ in range(2):
        t.forward(ancho)
        t.right(90)
        t.forward(alto)
        t.right(90)
    t.end_fill()

    # Franja Blanca
    t.forward(ancho)
    t.color("white")
    t.begin_fill()
    for _ in range(2):
        t.forward(ancho)
        t.right(90)
        t.forward(alto)
        t.right(90)
    t.end_fill()

    # Franja Roja
    t.forward(ancho)
    t.color("red")
    t.begin_fill()
    for _ in range(2):
        t.forward(ancho)
        t.right(90)
        t.forward(alto)
        t.right(90)
    t.end_fill()

    # Dibujar el círculo central (simbolizando el escudo)
    t.penup()
    t.goto(0, 0)
    t.pendown()
    t.color("black")
    t.circle(50)
    
    t.hideturtle()
    turtle.done()

dibujar_bandera()