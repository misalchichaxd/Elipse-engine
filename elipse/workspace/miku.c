#include <windows.h>
#include <gdiplus.h>
#pragma comment (lib,"gdiplus.lib")

// Estructura para pasar datos al hilo de animación
typedef struct {
    HWND hwnd;
    Gdiplus::Image* gifImage;
    UINT frameCount;
    UINT currentFrame;
    GUID dimensionID;
} GifAnimData;

// Hilo para refrescar la animación del GIF
DWORD WINAPI AnimateGifThread(LPVOID lpParam) {
    GifAnimData* data = (GifAnimData*)lpParam;
    PropertyItem* pItem = NULL;
    
    // Obtener el tiempo de demora de los frames si está disponible
    // Por simplicidad usaremos un delay fijo de ~60-80ms (aprox 12-15 fps)
    while (IsWindow(data->hwnd)) {
        InvalidateRect(data->hwnd, NULL, FALSE);
        Sleep(70);
    }
    return 0;
}

LRESULT CALLBACK WindowProc(HWND hwnd, UINT uMsg, WPARAM wParam, LPARAM lParam) {
    static Gdiplus::Image* mikuGif = NULL;
    static UINT frameCount = 0;
    static UINT currentFrame = 0;
    static GUID dimensionID;
    static HANDLE hThread = NULL;
    static GifAnimData animData;

    switch (uMsg) {
    case WM_CREATE: {
        // Cargar el GIF animado
        mikuGif = Gdiplus::Image::FromFile(L"miku.gif");
        if (mikuGif && mikuGif->GetLastStatus() == Gdiplus::Ok) {
            UINT count = mikuGif->GetFrameDimensionsCount();
            if (count > 0) {
                GUID* pDimensionIDs = new GUID[count];
                mikuGif->GetFrameDimensionsList(pDimensionIDs, count);
                dimensionID = pDimensionIDs[0];
                frameCount = mikuGif->GetFrameCount(&dimensionID);
                delete[] pDimensionIDs;
            }
            
            // Iniciar hilo para animar
            animData.hwnd = hwnd;
            animData.gifImage = mikuGif;
            animData.frameCount = frameCount;
            animData.dimensionID = dimensionID;
            hThread = CreateThread(NULL, 0, AnimateGifThread, &animData, 0, NULL);
        }
        break;
    }
    case WM_PAINT: {
        PAINTSTRUCT ps;
        HDC hdc = BeginPaint(hwnd, &ps);
        
        if (mikuGif && mikuGif->GetLastStatus() == Gdiplus::Ok) {
            Gdiplus::Graphics graphics(hdc);
            
            // Seleccionar el frame actual
            Gdiplus::Status status = mikuGif->SelectActiveFrame(&dimensionID, currentFrame);
            
            // Dibujar el frame centrado o en el cliente
            RECT rc;
            GetClientRect(hwnd, &rc);
            graphics.DrawImage(mikuGif, 0, 0, rc.right, rc.bottom);
            
            // Avanzar al siguiente frame
            if (frameCount > 0) {
                currentFrame = (currentFrame + 1) % frameCount;
            }
        } else {
            // Si no se encuentra el GIF, mostrar texto de aviso
            RECT rc;
            GetClientRect(hwnd, &rc);
            DrawTextW(hdc, L"Coloca un archivo 'miku.gif' en la misma carpeta.", -1, &rc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
        }
        
        EndPaint(hwnd, &ps);
        break;
    }
    case WM_DESTROY:
        if (mikuGif) delete mikuGif;
        PostQuitMessage(0);
        break;
    default:
        return DefWindowProc(hwnd, uMsg, wParam, lParam);
    }
    return 0;
}

int WINAPI WinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance, LPSTR lpCmdLine, int nCmdShow) {
    // Inicializar GDI+
    Gdiplus::GdiplusStartupInput gdiplusStartupInput;
    ULONG_PTR gdiplusToken;
    Gdiplus::GdiplusStartup(&gdiplusToken, &gdiplusStartupInput, NULL);

    // Registrar la clase de ventana
    const wchar_t CLASS_NAME[] = L"MikuWindowCls";
    WNDCLASSW wc = {};
    wc.lpfnWndProc = WindowProc;
    wc.hInstance = hInstance;
    wc.lpszClassName = CLASS_NAME;
    wc.hCursor = LoadCursor(NULL, IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);

    RegisterClassW(&wc);

    // Crear la ventana
    HWND hwnd = CreateWindowExW(
        0, CLASS_NAME, L"Miku Bailando - Ventana Normal",
        WS_OVERLAPPEDWINDOW,
        CW_USEDEFAULT, CW_USEDEFAULT, 500, 500,
        NULL, NULL, hInstance, NULL
    );

    if (hwnd == NULL) {
        return 0;
    }

    ShowWindow(hwnd, nCmdShow);

    // Bucle de mensajes estándar de Windows
    MSG msg = {};
    while (GetMessage(&msg, NULL, 0, 0)) {
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }

    // Apagar GDI+
    Gdiplus::GdiplusShutdown(gdiplusToken);
    return 0;
}
