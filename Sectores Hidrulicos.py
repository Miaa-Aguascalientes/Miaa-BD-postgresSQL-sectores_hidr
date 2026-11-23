import psycopg2
import mysql.connector
from psycopg2.extras import RealDictCursor
import tkinter as tk
from tkinter import ttk, messagebox
import folium
import webbrowser
import os
import json

# --- CONFIGURACIÓN DE CONEXIONES ---
CONFIG_POSTGRES = {
    'user': 'map_tecnica',
    'password': 'M144.Tec',
    'host': 'ti.miaa.mx',
    'database': 'qgis',
    'port': '5432'
}

CONFIG_MYSQL = {
    'user': 'miaamx_telemetria2',
    'password': 'bWkrw1Uum1O&',
    'host': 'miaa.mx',
    'database': 'miaamx_telemetria2'
}

class AppSectoresMIAA:
    def __init__(self, root):
        self.root = root
        self.root.title("MIAA - Sincronizador de Sectores (Coordenadas Limpias)")
        self.root.state('zoomed') 
        self.root.configure(bg="#1e1e1e")
        
        self.datos_locales = []
        self.todas_las_columnas = (
            "fid", "geom", "sector", "Pozos_Sector", "Cantidad_Pozos", "Superficie", 
            "Long_Red", "Vol_Prod", "U_Domesticos", "U_NoDom", "U_Tot", 
            "Poblacion", "Cons_m3", "Faltas_Agua", "Fugas_Tot", "FTC", 
            "FTA", "Vol_Medid", "Vol_Fact", "Kwh", "costoKw-hr", 
            "Recaudacion", "Dotacion", "Balance_Estimado"
        )
        
        self.estilo_interfaz()
        self.crear_componentes()

    def estilo_interfaz(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("Treeview", background="#2d2d2d", foreground="white", fieldbackground="#2d2d2d", borderwidth=0)
        style.map("Treeview", background=[('selected', '#005f73')])
        style.configure("Treeview.Heading", background="#3d3d3d", foreground="cyan", relief="flat")
        style.configure("TButton", padding=6, background="#008080", foreground="white")

    def crear_componentes(self):
        fm_top = ttk.Frame(self.root)
        fm_top.pack(fill="x", padx=20, pady=20)
        tk.Label(fm_top, text="MIAA - EXTRACCIÓN DE COORDENADAS", font=("Consolas", 14, "bold"), bg="#1e1e1e", fg="cyan").pack(side="left")
        
        ttk.Button(fm_top, text="1. Cargar y Limpiar", command=self.cargar_datos).pack(side="right", padx=5)
        self.btn_mapa = ttk.Button(fm_top, text="2. Verificar Mapa", command=self.generar_mapa_verificacion, state="disabled")
        self.btn_mapa.pack(side="right", padx=5)
        self.btn_sync = ttk.Button(fm_top, text="3. Sincronizar MySQL", command=self.sincronizar_datos, state="disabled")
        self.btn_sync.pack(side="right", padx=5)

        fm_tabla = ttk.Frame(self.root)
        fm_tabla.pack(fill="both", expand=True, padx=20, pady=10)
        self.tree = ttk.Treeview(fm_tabla, columns=self.todas_las_columnas, show='headings')
        
        scroll_y = ttk.Scrollbar(fm_tabla, orient="vertical", command=self.tree.yview)
        scroll_x = ttk.Scrollbar(fm_tabla, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        scroll_y.pack(side="right", fill="y"); scroll_x.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True)

        for col in self.todas_las_columnas:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=120, anchor="w")
        
        self.status_var = tk.StringVar(value="Listo")
        tk.Label(self.root, textvariable=self.status_var, bg="#1e1e1e", fg="#888").pack(side="bottom", anchor="w", padx=20)

    def cargar_datos(self):
        try:
            pg_conn = psycopg2.connect(**CONFIG_POSTGRES)
            pg_cursor = pg_conn.cursor(cursor_factory=RealDictCursor)
            
            # EXPLICACIÓN DE LA LIMPIEZA:
            # 1. ST_AsText convierte la geometría a texto.
            # 2. regexp_replace elimina cualquier letra (A-Z) y los paréntesis iniciales/finales.
            # 3. trim quita espacios sobrantes.
            query = """
                SELECT 
                    fid, 
                    trim(regexp_replace(ST_AsText(ST_Transform(geom, 4326)), '[A-Z\(\)]', '', 'g')) AS geom,
                    ST_AsGeoJSON(ST_Transform(geom, 4326)) AS geom_json,
                    sector, "Pozos_Sector", "Cantidad_Pozos", "Superficie", "Long_Red", "Vol_Prod", 
                    "U_Domesticos", "U_NoDom", "U_Tot", "Poblacion", "Cons_m3", 
                    "Faltas_Agua", "Fugas_Tot", "FTC", "FTA", "Vol_Medid", "Vol_Fact", 
                    "Kwh", "costoKw-hr", "Recaudacion", "Dotacion", "Balance_Estimado"
                FROM "Sectorizacion"."Sectores_hidr"
            """
            pg_cursor.execute(query)
            self.datos_locales = pg_cursor.fetchall()
            
            for i in self.tree.get_children(): self.tree.delete(i)
            for d in self.datos_locales:
                self.tree.insert("", "end", values=[d.get(col, "") for col in self.todas_las_columnas])
            
            pg_cursor.close(); pg_conn.close()
            self.status_var.set(f"Sectores procesados: {len(self.datos_locales)}")
            self.btn_sync.config(state="normal")
            self.btn_mapa.config(state="normal")
                
        except Exception as e:
            messagebox.showerror("Error Postgres", f"Fallo en limpieza de coordenadas:\n{str(e)}")

    def generar_mapa_verificacion(self):
        if not self.datos_locales: return
        m = folium.Map(location=[21.88, -102.28], zoom_start=12, tiles="cartodbpositron")
        
        for reg in self.datos_locales:
            if reg.get('geom_json'):
                try:
                    geo_data = json.loads(reg['geom_json'])
                    folium.GeoJson(
                        geo_data,
                        name=reg['sector'],
                        style_function=lambda x: {'fillColor': '#3388ff', 'color': '#0000ff', 'weight': 2, 'fillOpacity': 0.4},
                        tooltip=f"Sector: {reg['sector']}"
                    ).add_to(m)
                except: continue
        
        mapa_temp = "verificar_coordenadas.html"
        m.save(mapa_temp)
        webbrowser.open('file://' + os.path.realpath(mapa_temp))

    def sincronizar_datos(self):
        """
        Inserta las coordenadas como texto plano directamente en MySQL.
        Ideal para campos VARCHAR o TEXT.
        """
        if not self.datos_locales:
            messagebox.showwarning("MIAA", "No hay datos para insertar.")
            return

        try:
            # use_pure=True para compatibilidad con ejecutables
            my_conn = mysql.connector.connect(**CONFIG_MYSQL, use_pure=True)
            my_cursor = my_conn.cursor()
            
            columnas_sql = [f"`{col}`" for col in self.todas_las_columnas]
            
            # EL CAMBIO CLAVE:
            # Ya no usamos ST_GeomFromText. Mandamos el string de coordenadas
            # tal cual lo ves en la pantalla (texto plano).
            phs = ["%s"] * len(self.todas_las_columnas)
            
            query_mysql = f"REPLACE INTO Sectores_hidr ({', '.join(columnas_sql)}) VALUES ({', '.join(phs)})"
            
            datos_preparados = []
            for reg in self.datos_locales:
                # reg['geom'] ya viene limpio de Postgres gracias a la consulta con regexp_replace
                fila = tuple(reg.get(col) for col in self.todas_las_columnas)
                datos_preparados.append(fila)

            for i in range(0, len(datos_preparados), 50):
                my_cursor.executemany(query_mysql, datos_preparados[i:i+50])
                my_conn.commit()
            
            my_cursor.close()
            my_conn.close()
            
            messagebox.showinfo("MIAA", f"¡Ahora sí! Se insertaron {len(datos_preparados)} sectores como texto.")
                
        except Exception as e:
            messagebox.showerror("Error MySQL", f"Error al insertar texto:\n{str(e)}")
if __name__ == "__main__":
    root = tk.Tk()
    app = AppSectoresMIAA(root)
    root.mainloop()