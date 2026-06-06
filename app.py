"""
Bot de Google Forms - Versión Mejorada con detección robusta de GRIDS y PROBABILIDADES
Inspirado en la lógica de GFormTasker para cuadrículas

Instalación:
pip install flask flask-cors requests beautifulsoup4

Uso:
python app.py
"""

from flask import Flask, render_template_string, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import time
import random
import re
import json
import threading
from datetime import datetime

app = Flask(__name__)
CORS(app)

sessions = {}
statuses = {}

class GoogleFormBot:
    def __init__(self, form_url):
        self.form_url = form_url
        self.form_action = None
        self.fields = []
        self.all_entry_ids = []
        self.html_content = ''
        self.soup = None
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        self.page_count = 1
    
    def analyze_form(self):
        """Analiza el formulario usando AMBOS métodos: FB_PUBLIC_LOAD_DATA_ + HTML parsing"""
        try:
            response = self.session.get(self.form_url, timeout=10)
            response.raise_for_status()
            
            # Normalizar URL: eliminar /u/0/ que causa 404
            # https://docs.google.com/forms/u/0/d/e/XXXX/viewform
            # → https://docs.google.com/forms/d/e/XXXX/formResponse
            clean_url = re.sub(r'/forms/u/\d+/d/', '/forms/d/', self.form_url)
            clean_url = re.sub(r'/forms/u/\d+/', '/forms/', clean_url)
            self.form_action = clean_url.replace('/viewform', '/formResponse')
            if '/formResponse' not in self.form_action:
                # Si la URL no tenía /viewform, agregarlo al final
                base = clean_url.split('?')[0].rstrip('/')
                self.form_action = base + '/formResponse'
            
            print(f"  form_action: {self.form_action}")
            
            self.html_content = response.text
            self.soup = BeautifulSoup(self.html_content, 'html.parser')
            
            # Extraer TODOS los entry IDs del HTML
            all_entries = re.findall(r'entry\.(\d+)', self.html_content)
            seen = set()
            self.all_entry_ids = []
            for entry in all_entries:
                if entry not in seen:
                    seen.add(entry)
                    self.all_entry_ids.append(entry)
            
            print(f"\n{'='*70}")
            print(f"ANÁLISIS DEL FORMULARIO")
            print(f"{'='*70}")
            print(f"Total entry IDs encontrados: {len(self.all_entry_ids)}")
            print(f"Entry IDs: {self.all_entry_ids[:10]}{'...' if len(self.all_entry_ids) > 10 else ''}")
            
            # Intentar parsear FB_PUBLIC_LOAD_DATA_
            fb_data_match = re.search(r'FB_PUBLIC_LOAD_DATA_\s*=\s*(\[.*?\]);', response.text, re.DOTALL)
            
            if fb_data_match:
                print("\n✓ FB_PUBLIC_LOAD_DATA_ encontrado, parseando...")
                try:
                    json_str = fb_data_match.group(1)
                    form_data = json.loads(json_str)
                    
                    if len(form_data) > 1 and form_data[1]:
                        # Recopilar preguntas de TODAS las páginas del formulario
                        # form_data[1][1] son las preguntas de la página 1
                        # Páginas adicionales están en form_data[1][1] pero las secciones de página
                        # son elementos con type==8 que contienen sub-preguntas en su estructura.
                        # La forma correcta es recorrer TODOS los elementos del array form_data[1][1]
                        # y también buscar en form_data[1] nivel raíz por si hay múltiples secciones.
                        
                        all_questions = []
                        
                        def collect_questions(node):
                            """Recursivamente extrae preguntas de cualquier nivel del árbol JSON"""
                            if not isinstance(node, list):
                                return
                            # Detectar nodos de salto de página (type==8): sus preguntas hijas
                            # están en node[4][0] como lista de preguntas
                            try:
                                if len(node) > 3 and node[3] == 8:
                                    # Nodo de página: buscar preguntas en sus hijos
                                    for child in node:
                                        if isinstance(child, list):
                                            collect_questions(child)
                                    return
                            except (IndexError, TypeError):
                                pass
                            # Si parece una pregunta válida: tiene entry_id numérico en [4][0][0]
                            try:
                                if len(node) >= 4 and node[4] and isinstance(node[4], list) and node[4][0]:
                                    candidate_id = node[4][0][0]
                                    # Aceptar int o string numérico
                                    if candidate_id and (isinstance(candidate_id, int) or str(candidate_id).isdigit()):
                                        all_questions.append(node)
                                        return  # no seguir dentro de una pregunta
                            except (IndexError, TypeError):
                                pass
                            # Recursión en hijos
                            for item in node:
                                if isinstance(item, list):
                                    collect_questions(item)
                        
                        # Iterar todos los elementos del formulario (todas las páginas están en raw_items)
                        raw_items = form_data[1][1] if len(form_data[1]) > 1 else []
                        
                        # Contar páginas: cada nodo con type==8 es un salto de página
                        # 1 página base + cantidad de page_breaks
                        page_breaks = sum(1 for item in raw_items
                                         if isinstance(item, list) and len(item) > 3 and item[3] == 8)
                        self.page_count = page_breaks + 1
                        print(f"✓ Formulario con {self.page_count} página(s) detectadas")
                        
                        for item in raw_items:
                            if not item or not isinstance(item, list):
                                continue
                            collect_questions(item)
                        
                        print(f"✓ {len(all_questions)} preguntas encontradas en JSON (todas las páginas)")
                        
                        for question in all_questions:
                            if not question or len(question) < 4:
                                continue
                            
                            try:
                                entry_id = question[4][0][0] if question[4] and question[4][0] else None
                                question_text = self._best_label(question)
                                question_type = question[3] if len(question) > 3 else None
                                
                                if not entry_id:
                                    continue
                                
                                entry_id = str(entry_id)
                                field = self._parse_question_json(question, entry_id, question_text, question_type)
                                
                                if field:
                                    self.fields.append(field)
                            except (IndexError, TypeError, ValueError) as e:
                                continue
                        
                        # Detección mejorada de grids usando HTML parsing (refuerzo)
                        print("\n" + "="*70)
                        print("ANÁLISIS MEJORADO DE GRIDS (estilo GFormTasker)")
                        print("="*70)
                        self._enhance_grids_from_html()
                        
                        # Detección de campos de calificación (rating/estrellas)
                        self._detect_ratings_from_html()
                
                except json.JSONDecodeError:
                    return self._analyze_form_fallback(response.text)
            else:
                return self._analyze_form_fallback(response.text)
            
            print(f"\n{'='*70}")
            print(f"RESUMEN FINAL: {len(self.fields)} campos listos para envío")
            print(f"{'='*70}")
            for f in self.fields:
                if f['type'] in ['grid', 'grid_checkbox']:
                    print(f"  [GRID] {f['label'][:40]} | {len(f['rows'])} filas | {len(f['cols'])} cols | row_entries={f['row_entries']}")
                else:
                    print(f"  [{f['type'].upper()}] {f['label'][:40]} | name={f['name']} | opts={len(f.get('options',[]))}")
            print(f"{'='*70}\n")
            
            return {'success': True, 'fields': self.fields, 'count': len(self.fields)}
            
        except Exception as e:
            print(f"\n✗ ERROR: {str(e)}")
            return {'success': False, 'error': str(e)}
    
    def _detect_ratings_from_html(self):
        """
        Detecta preguntas de calificación (estrellas) desde el HTML.
        Google Forms las codifica como q_type==0 en el JSON pero tienen
        un elemento con aria-label que contiene 'star' o data-value numérico
        dentro de un contenedor de tipo rating.
        Busca también por el patrón data-params que incluye el tipo de widget.
        """
        print("\n⭐ Buscando campos de calificación en HTML...")

        # Estrategia 1: buscar por data-params con "rating" o por aria-label con "estrella"/"star"
        rating_entry_ids = set()

        # Buscar en el HTML raw: Google Forms incluye el tipo en FB_PUBLIC_LOAD_DATA_
        # como nodo con subtype "RATING" o similar, o en el HTML como [null,null,null,null,[[entry_id,...]],null,"RATING"]
        # Buscar patrones de rating en el JSON raw
        rating_matches = re.findall(
            r'\[\s*(\d{8,12})\s*,\s*\[\s*\[.*?\]\s*\]\s*,\s*\[\s*\[.*?\]\s*\]\s*,\s*(?:null\s*,\s*){0,3}"RATING"',
            self.html_content
        )
        for m in rating_matches:
            rating_entry_ids.add(m)
            print(f"  ✓ Rating detectado por patrón JSON: entry.{m}")

        # Estrategia 2: buscar en el HTML elementos con role="radio" dentro de contenedor
        # que tenga exactamente 5 opciones con valores 1-5 (sin texto, solo números)
        # y que NO sea un grid (los grids tienen filas y columnas separadas)
        containers = self.soup.find_all('div', attrs={'role': 'radiogroup'})
        for container in containers:
            radios = container.find_all('div', attrs={'role': 'radio'})
            if not radios:
                continue
            values = []
            for r in radios:
                v = r.get('data-value', '')
                if v:
                    values.append(v)
            # Patrón de rating: exactamente valores 1,2,3,4,5
            if sorted(values) == ['1', '2', '3', '4', '5']:
                # Buscar el entry ID asociado buscando en el HTML cercano
                html_str = str(container)
                found_ids = re.findall(r'entry\.(\d+)', html_str)
                for eid in found_ids:
                    rating_entry_ids.add(eid)
                    print(f"  ✓ Rating detectado por estructura 1-5: entry.{eid}")

        # Estrategia 3: buscar en el JSON raw nodos con exactamente 5 opciones numéricas 1-5
        # y q_type==0 (confundido con short_text)
        # Patrón: [[entry_id, [[1],[2],[3],[4],[5]], ...], ...]
        star_pattern = re.findall(
            r'\[(\d{7,12}),\s*\[\s*\[1\].*?\[2\].*?\[3\].*?\[4\].*?\[5\]\s*\]',
            self.html_content, re.DOTALL
        )
        for eid in star_pattern:
            rating_entry_ids.add(eid)
            print(f"  ✓ Rating detectado por opciones 1-5 en JSON: entry.{eid}")

        # Corregir los fields que correspondan
        corrected = 0
        for field in self.fields:
            entry_num = field['name'].replace('entry.', '')
            if entry_num in rating_entry_ids and field['type'] == 'short_text':
                field['type'] = 'rating'
                field['min_scale'] = 1
                field['max_scale'] = 5
                field['options'] = ['1', '2', '3', '4', '5']
                field['probabilities'] = [20.0] * 5
                field['mode'] = 'random'
                field['value'] = ''
                corrected += 1
                print(f"  ✅ Corregido: '{field['label']}' → rating")

        # Estrategia 4 (fallback): si hay campos short_text sin valor real
        # y en el HTML hay un contenedor de estrellas asociado al mismo entry,
        # buscar por aria-label que contenga "star" o "estrella"
        if corrected == 0:
            for field in self.fields:
                if field['type'] != 'short_text':
                    continue
                entry_num = field['name'].replace('entry.', '')
                # Buscar en el HTML si hay inputs de tipo radio con name=entry.X y valores 1-5
                pattern = rf'name="?entry\.{re.escape(entry_num)}"?[^>]*value="?([1-5])"?'
                radio_vals = re.findall(pattern, self.html_content)
                if sorted(set(radio_vals)) == ['1', '2', '3', '4', '5']:
                    field['type'] = 'rating'
                    field['min_scale'] = 1
                    field['max_scale'] = 5
                    field['options'] = ['1', '2', '3', '4', '5']
                    field['probabilities'] = [20.0] * 5
                    field['mode'] = 'random'
                    field['value'] = ''
                    corrected += 1
                    print(f"  ✅ Corregido (fallback radio): '{field['label']}' → rating")

        print(f"  → {corrected} campo(s) de calificación corregidos")

    def _enhance_grids_from_html(self):
        """
        NUEVA FUNCIÓN: Mejora la detección de grids usando parsing HTML
        Similar a cómo GFormTasker busca elementos en el DOM
        """
        print("\n🔍 Buscando grids en HTML...")
        
        # Buscar todas las tablas que son grids (tienen role="radiogroup" o "list")
        grid_containers = self.soup.find_all('div', class_=re.compile(r'.*grid.*', re.I))
        
        if not grid_containers:
            # Búsqueda alternativa por estructura
            grid_containers = self.soup.find_all('div', attrs={'role': ['radiogroup', 'group']})
        
        print(f"  → {len(grid_containers)} posibles grids encontrados")
        
        for idx, container in enumerate(grid_containers):
            print(f"\n  Grid candidato #{idx + 1}:")
            
            # Buscar filas (generalmente en <div> con aria-label)
            rows = []
            row_elements = container.find_all('div', attrs={'role': 'radio'})
            if not row_elements:
                row_elements = container.find_all('div', attrs={'role': 'checkbox'})
            
            # Extraer texto de filas desde aria-label o data-value
            for row_elem in row_elements:
                row_text = row_elem.get('aria-label', '').strip()
                if not row_text:
                    row_text = row_elem.get('data-value', '').strip()
                if not row_text:
                    # Buscar en el texto visible
                    row_text = row_elem.get_text(strip=True)
                
                if row_text and row_text not in rows:
                    rows.append(row_text)
            
            # Buscar columnas (headers de la tabla)
            cols = []
            header_elements = container.find_all('div', attrs={'role': 'columnheader'})
            if not header_elements:
                # Búsqueda alternativa
                header_elements = container.find_all('th')
            
            for col_elem in header_elements:
                col_text = col_elem.get_text(strip=True)
                if col_text and col_text not in cols:
                    cols.append(col_text)
            
            # Si encontramos filas y columnas, mejorar el field correspondiente
            # SOLO si el field aún no tiene datos completos del JSON
            if rows and cols:
                print(f"    ✓ Filas detectadas: {rows[:3]}{'...' if len(rows) > 3 else ''}")
                print(f"    ✓ Columnas detectadas: {cols}")
                
                for field in reversed(self.fields):
                    if field['type'] in ['grid', 'grid_checkbox']:
                        # No sobreescribir si ya tenemos row_entries del JSON
                        already_has_entries = bool(field.get('row_entries'))
                        if not field['rows'] or len(field['rows']) < len(rows):
                            print(f"    → Mejorando field '{field['label']}'")
                            field['rows'] = rows
                            field['cols'] = cols
                            if not already_has_entries:
                                print(f"    ⚠️  Sin entry IDs del JSON para este grid (página 2?)")
                            print(f"    ✓ Actualizado: {len(rows)} filas x {len(cols)} cols")
                            break
    
    def _best_label(self, q):
        """
        Elige el mejor texto de label para una pregunta del JSON de Google Forms.
          q[1] = título corto (subtema/grupo)
          q[2] = descripción completa, puede contener AMBOS separados por \\n
                 ej: 'Error en diagnóstico\\n1. ¿Considera usted...'
          q[4][0][3] = texto del campo individual (respaldo final)
        Lógica:
          1. Si q[2] contiene \\n → dividir y tomar la parte más larga
          2. Si q[2] es más largo que q[1] → usar q[2]
          3. Si no → usar q[1]
          4. Respaldo final: q[4][0][3], luego 'Campo'
        """
        q1, q2, q4_label = '', '', ''
        try:
            v = str(q[1]).strip()
            if v and v not in ('None', ''): q1 = v
        except: pass
        try:
            v = str(q[2]).strip()
            if v and v not in ('None', ''):
                if '\n' in v:
                    partes = [p.strip() for p in v.split('\n') if p.strip()]
                    q2 = max(partes, key=len) if partes else v
                else:
                    q2 = v
        except: pass
        try:
            v = str(q[4][0][3]).strip()
            if v and v not in ('None', ''): q4_label = v
        except: pass

        if q2 and q1:
            label = q2 if len(q2) > len(q1) else q1
        else:
            label = q2 or q1 or q4_label or 'Campo'
        return label

    def _parse_question_json(self, question, entry_id, question_text, question_type):
        """Parsea una pregunta desde FB_PUBLIC_LOAD_DATA_"""
        
        field = {
            'name': 'entry.' + str(entry_id),
            'label': str(question_text),
            'type': 'text',
            'question_type': int(question_type) if question_type is not None else 0,
            'mode': 'random',
            'value': '',
            'options': [],
            'probabilities': [],  # NUEVO: Array de probabilidades
            'rows': [],
            'cols': [],
            'row_entries': [],
            'min_scale': 1,
            'max_scale': 5,
            'checkbox_count': 1,
            'col_probabilities': []
        }
        
        try:
            q_type = int(question_type) if question_type is not None else 0
        except (ValueError, TypeError):
            q_type = 0
        
        if q_type == 0:
            field['type'] = 'short_text'
            field['mode'] = 'fixed'
            field['value'] = 'Respuesta automática'
        
        elif q_type == 1:
            field['type'] = 'paragraph'
            field['mode'] = 'fixed'
            field['value'] = 'Esta es una respuesta generada automáticamente.'
        
        elif q_type == 2:
            field['type'] = 'multiple_choice'
            if len(question) > 4 and question[4] and len(question[4]) > 0:
                choices = question[4][0][1]
                if choices:
                    field['options'] = [str(choice[0]) for choice in choices if choice and choice[0]]
                    # NUEVO: Inicializar probabilidades equitativas
                    field['probabilities'] = [round(100 / len(field['options']), 1) if field['options'] else 0] * len(field['options'])
        
        elif q_type == 3:
            field['type'] = 'dropdown'
            if len(question) > 4 and question[4] and len(question[4]) > 0:
                choices = question[4][0][1]
                if choices:
                    field['options'] = [str(choice[0]) for choice in choices if choice and choice[0]]
                    # NUEVO: Inicializar probabilidades equitativas
                    field['probabilities'] = [round(100 / len(field['options']), 1) if field['options'] else 0] * len(field['options'])
        
        elif q_type == 4:
            field['type'] = 'checkboxes'
            field['checkbox_count'] = 2
            if len(question) > 4 and question[4] and len(question[4]) > 0:
                choices = question[4][0][1]
                if choices:
                    field['options'] = [str(choice[0]) for choice in choices if choice and choice[0]]
                    # NUEVO: Inicializar probabilidades equitativas
                    field['probabilities'] = [round(100 / len(field['options']), 1) if field['options'] else 0] * len(field['options'])
        
        elif q_type == 5:
            field['type'] = 'linear_scale'
            if len(question) > 4 and question[4] and len(question[4]) > 0:
                scale_data = question[4][0]
                if len(scale_data) > 3:
                    try:
                        field['min_scale'] = int(scale_data[1]) if scale_data[1] else 1
                        field['max_scale'] = int(scale_data[2]) if scale_data[2] else 5
                        field['options'] = [str(i) for i in range(field['min_scale'], field['max_scale'] + 1)]
                        # NUEVO: Inicializar probabilidades equitativas
                        field['probabilities'] = [round(100 / len(field['options']), 1) if field['options'] else 0] * len(field['options'])
                    except (ValueError, TypeError):
                        field['min_scale'] = 1
                        field['max_scale'] = 5
                        field['options'] = ['1', '2', '3', '4', '5']
                        field['probabilities'] = [20.0] * 5
        
        elif q_type == 6:
            # Calificación con estrellas (Rating) - Google Forms tipo 6
            # Siempre es de 1 a 5 estrellas, se envía como escala lineal
            field['type'] = 'rating'
            field['min_scale'] = 1
            field['max_scale'] = 5
            field['options'] = ['1', '2', '3', '4', '5']
            field['probabilities'] = [20.0] * 5
            print(f"  ⭐ Campo de calificación (rating) detectado: '{question_text}'")

        elif q_type == 18:
            # Calificación con estrellas (Rating) - Google Forms tipo 18
            # Se envía igual que escala lineal: un número del 1 al 5
            field['type'] = 'rating'
            field['min_scale'] = 1
            field['max_scale'] = 5
            field['options'] = ['1', '2', '3', '4', '5']
            field['probabilities'] = [20.0] * 5
            print(f"  ⭐ Campo de calificación (rating tipo 18) detectado: '{question_text}'")

        elif q_type == 7 or q_type == 8:
            field['type'] = 'grid' if q_type == 7 else 'grid_checkbox'
            field['grid_type'] = 'radio' if q_type == 7 else 'checkbox'
            
            # CORRECCIÓN RAÍZ: En un grid de Google Forms, question[4] es una lista donde
            # CADA elemento corresponde a UNA FILA, y tiene la forma:
            #   [ entry_id_de_esa_fila, [ [col1_texto,...], [col2_texto,...], ... ], [fila_texto,...], ... ]
            # Por eso hay que iterar question[4] completo, no solo [4][0].
            
            if len(question) > 4 and question[4]:
                print(f"\n  📊 Analizando grid '{question_text}' (tipo {q_type}):")
                print(f"    question[4] tiene {len(question[4])} sub-entradas")
                
                cols_extracted = []
                rows_extracted = []
                row_entry_ids = []
                
                for row_data in question[4]:
                    if not row_data:
                        continue
                    
                    # Primer elemento: entry_id de esta fila
                    try:
                        row_entry_id = str(row_data[0])
                        if row_entry_id.isdigit():
                            row_entry_ids.append(row_entry_id)
                    except (IndexError, TypeError):
                        pass
                    
                    # Segundo elemento: opciones de columna (si existen y cols aún no extraídas)
                    if not cols_extracted:
                        try:
                            if len(row_data) > 1 and row_data[1]:
                                for col_item in row_data[1]:
                                    if col_item and len(col_item) > 0 and col_item[0]:
                                        col_text = str(col_item[0])
                                        if col_text not in cols_extracted:
                                            cols_extracted.append(col_text)
                        except (IndexError, TypeError):
                            pass
                    
                    # Tercer elemento: texto de la fila
                    try:
                        if len(row_data) > 2 and row_data[2]:
                            for row_item in row_data[2]:
                                if row_item and len(row_item) > 0 and row_item[0]:
                                    row_text = str(row_item[0])
                                    if row_text not in rows_extracted:
                                        rows_extracted.append(row_text)
                    except (IndexError, TypeError):
                        pass
                
                field['cols'] = cols_extracted
                field['rows'] = rows_extracted
                # Asignar entry_ids por fila DIRECTO desde el JSON (sin depender del HTML)
                field['row_entries'] = row_entry_ids
                
                print(f"    ✓ {len(field['rows'])} filas, {len(field['cols'])} columnas")
                print(f"    ✓ Entry IDs por fila: {field['row_entries']}")
                
                # Si no se extrajeron filas con etiquetas, crear nombres genéricos
                if not field['rows'] and field['row_entries']:
                    field['rows'] = [f'Fila {i+1}' for i in range(len(field['row_entries']))]
                    print(f"    ⚠️  Filas sin texto, usando nombres genéricos")
                
                # Inicializar probabilidades por columna
                if field['cols']:
                    equal_prob = round(100 / len(field['cols']), 1)
                    field['col_probabilities'] = [equal_prob] * len(field['cols'])
        
        elif q_type == 9:
            field['type'] = 'date'
            field['mode'] = 'fixed'
            field['value'] = datetime.now().strftime('%Y-%m-%d')
        
        elif q_type == 10:
            field['type'] = 'time'
            field['mode'] = 'fixed'
            field['value'] = datetime.now().strftime('%H:%M')
        
        if not field['options'] and not field['rows'] and field['type'] not in ['short_text', 'paragraph', 'date', 'time', 'rating']:
            field['type'] = 'short_text'
            field['mode'] = 'fixed'
        
        return field
    
    def _assign_grid_entries(self):
        """Asigna los entry IDs correctos a cada fila de las cuadrículas"""
        used_entries = set()
        grid_fields = []
        
        for field in self.fields:
            if field['type'] in ['grid', 'grid_checkbox']:
                grid_fields.append(field)
            else:
                entry_num = field['name'].replace('entry.', '')
                used_entries.add(entry_num)
        
        available_entries = [e for e in self.all_entry_ids if e not in used_entries]
        
        print(f"\n{'='*70}")
        print(f"ASIGNACIÓN DE ENTRY IDS A GRIDS")
        print(f"{'='*70}")
        print(f"Entry IDs disponibles: {available_entries[:10]}{'...' if len(available_entries) > 10 else ''}")
        print(f"Grids a procesar: {len(grid_fields)}")
        
        entry_index = 0
        for field in grid_fields:
            num_rows = len(field['rows'])
            
            if num_rows == 0:
                print(f"\n⚠️  Grid '{field['label']}': Sin filas detectadas, saltando")
                continue
            
            if entry_index + num_rows <= len(available_entries):
                field['row_entries'] = available_entries[entry_index:entry_index + num_rows]
                
                print(f"\n✓ Grid '{field['label']}':")
                print(f"  Tipo: {field['grid_type']}")
                print(f"  Filas ({num_rows}): {field['rows'][:3]}{'...' if num_rows > 3 else ''}")
                print(f"  Columnas ({len(field['cols'])}): {field['cols']}")
                print(f"  Entry IDs: {field['row_entries']}")
                
                entry_index += num_rows
            else:
                print(f"\n✗ Grid '{field['label']}': Faltan entry IDs")
                print(f"   Necesita: {num_rows}, Disponibles: {len(available_entries) - entry_index}")
    
    def _analyze_form_fallback(self, html):
        """Método alternativo de análisis"""
        entries = re.findall(r'entry\.(\d+)', html)
        entries = list(set(entries))
        
        for entry_id in entries:
            field = {
                'name': 'entry.' + str(entry_id),
                'label': 'Campo entry.' + str(entry_id),
                'type': 'short_text',
                'mode': 'fixed',
                'value': 'Respuesta',
                'options': [],
                'probabilities': [],
                'rows': [],
                'cols': [],
                'row_entries': [],
                'checkbox_count': 1
            }
            self.fields.append(field)
        
        return {'success': True, 'fields': self.fields, 'count': len(entries)}
    
    def generate_value(self, field):
        """Genera un valor para el campo según su tipo y configuración"""
        
        # Mapa de niveles a pesos numéricos
        LEVEL_WEIGHTS = {'nulo': 0, 'bajo': 1, 'medio': 3, 'alto': 9}
        
        def levels_to_weights(levels):
            return [LEVEL_WEIGHTS.get(str(l).lower(), 1) for l in levels]
        
        if field['mode'] == 'fixed':
            value = field['value']
            if isinstance(value, str) and '<gft>' in value:
                values = [v.strip() for v in value.split('<gft>') if v.strip()]
                if values:
                    value = random.choice(values)
            if field['type'] in ['multiple_choice', 'dropdown', 'linear_scale', 'rating']:
                return str(value) if value else ''
            return value
        
        # Modo aleatorio con niveles de probabilidad
        if field['type'] in ['multiple_choice', 'dropdown', 'linear_scale', 'rating']:
            if field['options']:
                levels = field.get('probabilities', [])
                weights = levels_to_weights(levels) if levels else None
                # Filtrar opciones con peso > 0
                if weights:
                    valid = [(o, w) for o, w in zip(field['options'], weights) if w > 0]
                    if valid:
                        opts, wts = zip(*valid)
                        return str(random.choices(list(opts), weights=list(wts), k=1)[0])
                return str(random.choice(field['options']))
            return str(field['value']) if field['value'] else ''
        
        if field['type'] == 'checkboxes':
            if field['options']:
                if field['mode'] == 'random':
                    levels = field.get('probabilities', [])
                    weights = levels_to_weights(levels) if levels else None
                    # Candidatos con peso > 0
                    if weights:
                        valid = [(o, w) for o, w in zip(field['options'], weights) if w > 0]
                    else:
                        valid = [(o, 1) for o in field['options']]
                    count = min(field.get('checkbox_count', 1), len(valid))
                    if valid and count > 0:
                        opts, wts = zip(*valid)
                        selected = random.choices(list(opts), weights=list(wts), k=count)
                        # Evitar duplicados manteniendo orden de pesos
                        seen = set(); unique = []
                        for s in selected:
                            if s not in seen:
                                seen.add(s); unique.append(s)
                        return [str(o) for o in unique]
                    return []
                elif field['value']:
                    if isinstance(field['value'], list):
                        return [str(v) for v in field['value']]
                    else:
                        return [str(field['value'])]
            return []
        
        if field['type'] in ['grid', 'grid_checkbox']:
            return self._generate_grid_values(field)
        
        # short_text / paragraph en modo random: aplicar <gft> si existe
        value = str(field['value']) if field['value'] else ''
        if '<gft>' in value:
            parts = [p.strip() for p in value.split('<gft>') if p.strip()]
            if parts:
                value = random.choice(parts)
        return value
    
    def _weighted_choice(self, options, probabilities):
        """Selecciona una opción basada en probabilidades"""
        # Normalizar probabilidades
        total = sum(probabilities)
        if total == 0:
            return random.choice(options)
        
        normalized = [p / total for p in probabilities]
        return random.choices(options, weights=normalized, k=1)[0]
    
    def _weighted_sample(self, options, probabilities, k):
        """Selecciona múltiples opciones basadas en probabilidades"""
        # Normalizar probabilidades
        total = sum(probabilities)
        if total == 0:
            return random.sample(options, k)
        
        normalized = [p / total for p in probabilities]
        return random.choices(options, weights=normalized, k=k)
    
    def _generate_grid_values(self, field):
        """Genera valores para cuadrícula usando niveles de probabilidad"""
        grid_values = {}
        LEVEL_WEIGHTS = {'nulo': 0, 'bajo': 1, 'medio': 3, 'alto': 9}
        
        if not field['rows'] or not field['cols']:
            return grid_values
        
        col_levels = field.get('col_probabilities', [])
        col_weights = [LEVEL_WEIGHTS.get(str(l).lower(), 1) for l in col_levels] if col_levels else None
        valid_cols = [(c, w) for c, w in zip(field['cols'], col_weights)] if col_weights else [(c, 1) for c in field['cols']]
        valid_cols = [(c, w) for c, w in valid_cols if w > 0]
        
        if not valid_cols:
            valid_cols = [(c, 1) for c in field['cols']]
        
        cols_v, weights_v = zip(*valid_cols)
        
        if field.get('grid_type') == 'checkbox':
            for row in field['rows']:
                if field['mode'] == 'random':
                    num = random.randint(1, min(3, len(cols_v)))
                    selected = random.choices(list(cols_v), weights=list(weights_v), k=num)
                    grid_values[row] = list(set(str(c) for c in selected))
                else:
                    grid_values[row] = [str(field['cols'][0])] if field['cols'] else []
        else:
            for row in field['rows']:
                if field['mode'] == 'random':
                    grid_values[row] = str(random.choices(list(cols_v), weights=list(weights_v), k=1)[0])
                else:
                    grid_values[row] = str(field['cols'][0]) if field['cols'] else ''
        
        return grid_values
    
    def submit_form(self):
        """Envía el formulario una vez - soporta formularios multi-página"""
        data = {}
        
        print(f"\n{'='*50}")
        print(f"ENVIANDO FORMULARIO")
        print(f"{'='*50}")
        
        for field in self.fields:
            value = self.generate_value(field)
            
            if not value and value != 0:
                continue
            
            if field['type'] == 'checkboxes' and isinstance(value, list):
                if value:
                    data[field['name']] = value
            
            elif field['type'] in ['grid', 'grid_checkbox'] and isinstance(value, dict):
                row_entries = field.get('row_entries', [])
                
                for row_idx, (row_name, col_value) in enumerate(value.items()):
                    if field['type'] == 'grid_checkbox' and isinstance(col_value, list):
                        if row_idx < len(row_entries):
                            row_entry = 'entry.' + str(row_entries[row_idx])
                            data[row_entry] = col_value
                    else:
                        if row_idx < len(row_entries):
                            row_entry = 'entry.' + str(row_entries[row_idx])
                            data[row_entry] = str(col_value)
            
            else:
                data[field['name']] = str(value)
        
        try:
            form_data = []
            
            # pageHistory: indica a Google Forms que se navegó por todas las páginas.
            # Sin esto, en formularios multi-página solo se registran las respuestas
            # de la primera página. El valor es "0" para página 1 sola,
            # "0,1" para 2 páginas, "0,1,2" para 3, etc.
            # Detectamos cuántas páginas hay desde self.page_count (calculado en analyze)
            page_count = getattr(self, 'page_count', 1)
            page_history = ','.join(str(i) for i in range(page_count))
            form_data.append(('pageHistory', page_history))
            
            for key, val in data.items():
                if isinstance(val, list):
                    for v in val:
                        form_data.append((key, str(v)))
                else:
                    form_data.append((key, str(val)))
            
            print(f"  Enviando {len(form_data)} campos a: {self.form_action}")
            print(f"  pageHistory: {page_history}")
            for k, v in form_data:
                print(f"    {k} = {v}")
            
            response = self.session.post(
                self.form_action,
                data=form_data,
                allow_redirects=True,
                timeout=10,
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Referer': self.form_url,
                    'Origin': 'https://docs.google.com',
                }
            )
            
            if response.status_code == 200:
                # Verificar que no sea redirección a error
                if 'formResponse' in response.url or 'docs.google.com' in response.url:
                    print(f"✅ Envío exitoso (url final: {response.url[:80]})")
                    return True, dict(form_data)
                else:
                    print(f"⚠️  Respuesta 200 pero URL inesperada: {response.url[:80]}")
                    return True, dict(form_data)
            else:
                print(f"✗ Error HTTP: {response.status_code} - URL: {response.url[:80]}")
                return False, None
                
        except Exception as e:
            print(f"✗ Excepción: {str(e)}")
            return False, None


HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zhero Bot</title>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@700;900&family=Inter:wght@400;600&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Inter', 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh; padding: 20px;
        }

        /* ══════════════════════════════════════
           PANTALLA DE CARGA — ZHERO BOT
        ══════════════════════════════════════ */
        #splashScreen {
            position: fixed; inset: 0; z-index: 9999;
            background: #0a0a0f;
            display: flex; flex-direction: column;
            align-items: center; justify-content: center;
            transition: opacity 0.8s ease, visibility 0.8s ease;
        }
        #splashScreen.hidden {
            opacity: 0; visibility: hidden; pointer-events: none;
        }

        /* Partículas de fondo */
        .splash-bg {
            position: absolute; inset: 0; overflow: hidden;
        }
        .particle {
            position: absolute; border-radius: 50%;
            background: rgba(102,126,234,0.15);
            animation: float linear infinite;
        }
        @keyframes float {
            0%   { transform: translateY(110vh) scale(0); opacity: 0; }
            10%  { opacity: 1; }
            90%  { opacity: 1; }
            100% { transform: translateY(-10vh) scale(1.2); opacity: 0; }
        }

        /* Logo central */
        .splash-content {
            position: relative; z-index: 2;
            display: flex; flex-direction: column;
            align-items: center; gap: 0;
        }

        /* Anillo giratorio exterior */
        .splash-ring {
            width: 160px; height: 160px;
            border-radius: 50%;
            border: 3px solid transparent;
            border-top-color: #667eea;
            border-right-color: #764ba2;
            animation: spin 1.5s linear infinite;
            position: absolute;
            top: 50%; left: 50%;
            transform: translate(-50%, -50%);
        }
        .splash-ring-2 {
            width: 140px; height: 140px;
            border-radius: 50%;
            border: 2px solid transparent;
            border-bottom-color: #a78bfa;
            border-left-color: #667eea;
            animation: spin 2s linear infinite reverse;
            position: absolute;
            top: 50%; left: 50%;
            transform: translate(-50%, -50%);
        }
        @keyframes spin { to { transform: translate(-50%,-50%) rotate(360deg); } }

        /* Icono robot en el centro */
        .splash-icon-wrap {
            width: 160px; height: 160px;
            position: relative; margin-bottom: 36px;
        }
        .splash-icon {
            width: 100px; height: 100px;
            background: linear-gradient(135deg, #667eea, #764ba2);
            border-radius: 24px;
            display: flex; align-items: center; justify-content: center;
            font-size: 48px;
            position: absolute; top: 50%; left: 50%;
            transform: translate(-50%, -50%);
            box-shadow: 0 0 40px rgba(102,126,234,0.5);
            animation: pulse-glow 2s ease-in-out infinite;
        }
        @keyframes pulse-glow {
            0%, 100% { box-shadow: 0 0 30px rgba(102,126,234,0.4); }
            50%       { box-shadow: 0 0 60px rgba(118,75,162,0.7); }
        }

        /* Nombre del bot */
        .splash-title {
            font-family: 'Orbitron', monospace;
            font-size: clamp(38px, 8vw, 72px);
            font-weight: 900;
            letter-spacing: 6px;
            background: linear-gradient(90deg, #667eea, #a78bfa, #764ba2, #667eea);
            background-size: 300% 100%;
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            animation: shimmer 3s linear infinite;
            text-align: center;
            line-height: 1.1;
        }
        @keyframes shimmer {
            0%   { background-position: 0% 50%; }
            100% { background-position: 300% 50%; }
        }

        .splash-subtitle {
            font-family: 'Inter', sans-serif;
            font-size: clamp(12px, 2.5vw, 15px);
            color: rgba(255,255,255,0.45);
            letter-spacing: 4px;
            text-transform: uppercase;
            margin-top: 10px;
            text-align: center;
        }

        /* Barra de progreso */
        .splash-progress-wrap {
            margin-top: 52px;
            width: clamp(220px, 55vw, 380px);
        }
        .splash-progress-track {
            width: 100%; height: 4px;
            background: rgba(255,255,255,0.08);
            border-radius: 99px; overflow: hidden;
        }
        .splash-progress-bar {
            height: 100%; width: 0%;
            background: linear-gradient(90deg, #667eea, #a78bfa);
            border-radius: 99px;
            transition: width 0.25s ease;
            box-shadow: 0 0 12px rgba(102,126,234,0.8);
        }
        .splash-status {
            margin-top: 14px;
            font-size: 12px;
            color: rgba(255,255,255,0.35);
            letter-spacing: 2px;
            text-transform: uppercase;
            text-align: center;
            min-height: 18px;
            font-family: 'Inter', monospace;
        }

        /* Versión */
        .splash-version {
            position: absolute; bottom: 28px;
            font-size: 11px; color: rgba(255,255,255,0.2);
            letter-spacing: 2px; font-family: 'Inter', sans-serif;
        }

        /* ── Ocultar contenido principal durante la carga ── */
        #mainContent { display: none; }
        .container { max-width: 1200px; margin: 0 auto; }
        .card {
            background: white; border-radius: 15px; padding: 30px;
            margin-bottom: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); position: relative;
        }
        h1 { color: #667eea; margin-bottom: 10px; }
        h2 { color: #333; margin-bottom: 15px; }
        .sv-watermark {
            position: absolute; top: 20px; right: 20px;
            font-size: 24px; font-weight: bold;
            color: rgba(102,126,234,0.7); pointer-events: none; z-index: 10;
        }
        .success-badge {
            background: #10b981; color: white; padding: 8px 16px;
            border-radius: 20px; font-size: 13px; display: inline-block; margin: 10px 0;
        }
        .warning {
            background: #fff3cd; border-left: 4px solid #ffc107;
            padding: 15px; margin: 20px 0; border-radius: 5px; line-height: 1.6;
        }
        .form-group { margin-bottom: 20px; }
        label { display: block; font-weight: 600; margin-bottom: 8px; color: #333; font-size: 13px; }
        input[type="text"], input[type="number"], input[type="date"], input[type="time"], select, textarea {
            width: 100%; padding: 12px; border: 2px solid #e0e0e0;
            border-radius: 8px; font-size: 14px; transition: border 0.3s;
        }
        textarea { min-height: 80px; resize: vertical; }
        input:focus, select:focus, textarea:focus { outline: none; border-color: #667eea; }
        .input-group { display: flex; gap: 10px; }
        .input-group input[type="text"] { flex: 1; min-width: 0; }
        button {
            padding: 12px 24px; border: none; border-radius: 8px;
            font-weight: 600; cursor: pointer; transition: all 0.3s; font-size: 14px;
        }
        .btn-primary { background: #667eea; color: white; white-space: nowrap; }
        .btn-primary:hover { background: #5568d3; }
        .btn-success { background: #10b981; color: white; }
        .btn-danger { background: #ef4444; color: white; }
        button:disabled { opacity: 0.5; cursor: not-allowed; }
        .fields-container { max-height: 700px; overflow-y: auto; padding-right: 10px; }
        .field-item {
            background: #f9fafb; border: 2px solid #e5e7eb;
            border-radius: 10px; padding: 20px; margin-bottom: 15px;
        }
        .field-header {
            display: flex; justify-content: space-between;
            align-items: flex-start; margin-bottom: 15px; gap: 10px; flex-wrap: wrap;
        }
        .field-title { font-weight: 600; color: #1f2937; font-size: 15px; flex: 1; min-width: 0; word-break: break-word; }
        .field-type-badge {
            background: #667eea; color: white; padding: 4px 12px;
            border-radius: 12px; font-size: 11px; white-space: nowrap; flex-shrink: 0;
        }
        .badge-grid { background: #f59e0b; }
        .badge-checkbox { background: #10b981; }
        .btn-mode {
            display: flex; align-items: center; gap: 6px;
            padding: 7px 16px; border: none; border-radius: 20px;
            font-size: 12px; font-weight: 700; cursor: pointer;
            white-space: nowrap; transition: all 0.2s; flex-shrink: 0;
        }
        .btn-mode-random { background: #10b981; color: white; }
        .btn-mode-fixed  { background: #667eea; color: white; }

        /* ── Sistema de Niveles de Probabilidad ── */
        .prob-container {
            background: white; border: 2px solid #e5e7eb;
            border-radius: 10px; padding: 14px; margin-top: 14px;
        }
        .prob-header {
            display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 12px; padding-bottom: 10px; border-bottom: 2px solid #f3f4f6;
        }
        .prob-header h4 { color: #667eea; font-size: 13px; margin: 0; }
        .prob-row {
            display: flex; align-items: center; gap: 10px;
            padding: 8px 6px; border-radius: 7px; margin-bottom: 6px; background: #f9fafb;
            flex-wrap: wrap;
        }
        .prob-row:hover { background: #f3f4f6; }
        .prob-label {
            flex: 1; font-size: 13px; color: #374151; font-weight: 500;
            word-break: break-word; min-width: 0;
        }
        .prob-levels { display: flex; gap: 5px; flex-wrap: wrap; flex-shrink: 0; }
        .level-btn {
            padding: 5px 10px; border: 2px solid transparent;
            border-radius: 20px; font-size: 11px; font-weight: 700;
            cursor: pointer; transition: all 0.15s; letter-spacing: 0.3px;
        }
        .level-btn:hover { transform: scale(1.08); }
        .level-nulo  { background: #f1f5f9; color: #94a3b8; border-color: #e2e8f0; }
        .level-bajo  { background: #fef3c7; color: #92400e; border-color: #fde68a; }
        .level-medio { background: #dbeafe; color: #1e40af; border-color: #bfdbfe; }
        .level-alto  { background: #d1fae5; color: #065f46; border-color: #a7f3d0; }
        .level-nulo.active  { background: #64748b; color: white; border-color: #475569; }
        .level-bajo.active  { background: #f59e0b; color: white; border-color: #d97706; }
        .level-medio.active { background: #3b82f6; color: white; border-color: #2563eb; }
        .level-alto.active  { background: #10b981; color: white; border-color: #059669; }
        .prob-actions {
            display: flex; gap: 8px; margin-top: 12px;
            padding-top: 10px; border-top: 1px solid #e5e7eb; flex-wrap: wrap;
        }
        .btn-prob-action {
            display: flex; align-items: center; gap: 6px;
            padding: 8px 18px; border: none; border-radius: 8px;
            font-size: 13px; font-weight: 700; cursor: pointer; transition: all 0.2s;
        }
        .btn-prob-action svg { width: 15px; height: 15px; flex-shrink: 0; }
        .btn-igualar  { background: #667eea; color: white; }
        .btn-igualar:hover  { background: #5568d3; }
        .btn-aleatorio { background: #10b981; color: white; }
        .btn-aleatorio:hover { background: #059669; }
        /* ── Grid ── */
        .grid-preview { background: white; padding: 15px; border-radius: 6px; border: 1px solid #e5e7eb; overflow-x: auto; }
        .grid-table { width: 100%; border-collapse: collapse; font-size: 13px; min-width: 300px; }
        .grid-table th, .grid-table td { padding: 8px; border: 1px solid #e5e7eb; text-align: left; }
        .grid-table th { background: #f3f4f6; font-weight: 600; }
        /* ── Progress ── */
        .progress-bar { width: 100%; height: 30px; background: #e5e7eb; border-radius: 15px; overflow: hidden; margin: 20px 0; }
        .progress-fill {
            height: 100%; background: linear-gradient(90deg, #667eea, #764ba2);
            transition: width 0.3s; display: flex; align-items: center;
            justify-content: center; color: white; font-weight: 600;
        }
        .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin: 20px 0; }
        .stat-box {
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white; padding: 20px; border-radius: 10px; text-align: center;
        }
        .stat-box h3 { font-size: 32px; margin-bottom: 5px; }
        .logs {
            background: #1f2937; color: #e5e7eb; padding: 20px;
            border-radius: 10px; height: 300px; overflow-y: auto;
            font-family: 'Courier New', monospace; font-size: 13px;
        }
        .log-entry { margin-bottom: 8px; padding: 5px; border-left: 3px solid transparent; }
        .log-success { border-color: #10b981; color: #6ee7b7; }
        .log-error   { border-color: #ef4444; color: #fca5a5; }
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }
        .checkbox-qty { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
        .checkbox-qty input { width: 70px; }

        /* ── Responsive: Tablet (≤768px) ── */
        @media (max-width: 768px) {
            body { padding: 12px; }
            .card { padding: 20px; border-radius: 12px; }
            h1 { font-size: 22px; }
            h2 { font-size: 18px; }
            .sv-watermark { font-size: 18px; top: 14px; right: 14px; }
            .input-group { flex-direction: column; }
            .input-group button { width: 100%; }
            .grid-2 { grid-template-columns: 1fr; gap: 0; }
            .stats { grid-template-columns: repeat(2, 1fr); gap: 10px; }
            .stat-box { padding: 15px; }
            .stat-box h3 { font-size: 26px; }
            .fields-container { max-height: 600px; padding-right: 4px; }
            .prob-row { flex-direction: column; align-items: flex-start; gap: 6px; }
            .prob-levels { flex-wrap: wrap; }
            .btn-prob-action { padding: 8px 12px; font-size: 12px; }
            .logs { height: 220px; font-size: 12px; padding: 14px; }
        }

        /* ── Responsive: Móvil (≤480px) ── */
        @media (max-width: 480px) {
            body { padding: 8px; }
            .card { padding: 14px; border-radius: 10px; margin-bottom: 12px; }
            h1 { font-size: 18px; }
            h2 { font-size: 16px; }
            .sv-watermark { font-size: 15px; top: 10px; right: 10px; }
            .success-badge { font-size: 11px; padding: 6px 12px; }
            .warning { padding: 10px; font-size: 13px; }
            label { font-size: 12px; }
            input[type="text"], input[type="number"], input[type="date"],
            input[type="time"], select, textarea { padding: 10px; font-size: 13px; }
            button { padding: 10px 16px; font-size: 13px; }
            .stats { grid-template-columns: repeat(2, 1fr); gap: 8px; margin: 12px 0; }
            .stat-box { padding: 12px 8px; border-radius: 8px; }
            .stat-box h3 { font-size: 22px; margin-bottom: 2px; }
            .stat-box p { font-size: 11px; }
            .field-item { padding: 12px; }
            .field-title { font-size: 13px; }
            .field-type-badge { font-size: 10px; padding: 3px 8px; }
            .btn-mode { padding: 6px 12px; font-size: 11px; }
            .prob-container { padding: 10px; }
            .prob-row { flex-direction: column; align-items: flex-start; gap: 5px; padding: 10px 8px; }
            .prob-label { font-size: 12px; width: 100%; }
            .prob-levels { flex-wrap: wrap; gap: 6px; }
            .level-btn { padding: 5px 10px; font-size: 11px; }
            .prob-actions { gap: 6px; }
            .btn-prob-action { padding: 7px 10px; font-size: 11px; flex: 1; justify-content: center; }
            .fields-container { max-height: 500px; padding-right: 2px; }
            .logs { height: 180px; font-size: 11px; padding: 10px; }
            .progress-bar { height: 24px; }
            #startBtn, #stopBtn { width: 100%; margin-bottom: 8px; }
        }
    </style>
</head>
<body>

<!-- ══════════════════════════════════════
     PANTALLA DE CARGA — ZHERO BOT
══════════════════════════════════════ -->
<div id="splashScreen">
    <!-- Partículas de fondo -->
    <div class="splash-bg" id="splashBg"></div>

    <!-- Contenido central -->
    <div class="splash-content">
        <div class="splash-icon-wrap">
            <div class="splash-ring"></div>
            <div class="splash-ring-2"></div>
            <div class="splash-icon">🤖</div>
        </div>

        <div class="splash-title">ZHERO</div>
        <div style="font-family:'Orbitron',monospace; font-size:clamp(20px,4vw,36px); font-weight:700;
                    color:rgba(255,255,255,0.85); letter-spacing:10px; margin-top:2px; text-align:center;">
            BOT
        </div>
        <div class="splash-subtitle">Google Forms Automation</div>

        <div class="splash-progress-wrap">
            <div class="splash-progress-track">
                <div class="splash-progress-bar" id="splashBar"></div>
            </div>
            <div class="splash-status" id="splashStatus">Iniciando sistema...</div>
        </div>
    </div>

    <div class="splash-version">v2.0 &nbsp;·&nbsp; by SV</div>
</div>

<!-- ══════════════════════════════════════
     CONTENIDO PRINCIPAL
══════════════════════════════════════ -->
<div id="mainContent">
<div class="container">
    <div class="card">
        <div class="sv-watermark">SV</div>

        <div class="welcome-box" style="background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;border-radius:12px;padding:18px 22px;margin-bottom:18px;text-align:center;">
            <h2 style="font-size:20px;margin-bottom:6px;color:white;">&#x1F916; Bienvenido/a a Zhero Bot</h2>
            <p style="font-size:13px;opacity:0.92;margin:0;line-height:1.6;">Tu herramienta inteligente para automatizar respuestas en Google Forms.<br>
            Solo configura tus opciones y probabilidades, y deja que Zhero Bot haga el resto.</p>
        </div>

        <div class="success-badge">&#x2705; Multi-pagina + Niveles de Probabilidad</div>
        <div class="warning"><strong>&#x26A0;&#xFE0F; Uso Responsable:</strong> Usa esta herramienta solo en formularios propios o con autorización.</div>

        <button onclick="toggleHelp()" style="background:none;border:2px solid #667eea;color:#667eea;border-radius:8px;padding:8px 18px;font-size:13px;font-weight:700;cursor:pointer;margin-bottom:4px;">&#x2753; ¿Cómo usar Zhero Bot?</button>
        <div id="helpBox" style="display:none;background:#f0f4ff;border:2px solid #c7d2fe;border-radius:10px;padding:18px;margin-top:12px;line-height:1.7;">
            <h4 style="color:#667eea;margin:0 0 5px;font-size:13px;">&#x2699;&#xFE0F; Configuración previa del formulario</h4>
            <p style="font-size:13px;color:#374151;">Para que Zhero Bot funcione correctamente, configura tu Google Form así:</p>
            <ul style="padding-left:18px;margin:4px 0 10px;font-size:13px;color:#374151;">
                <li>&#x274C; <strong>Desactiva</strong> "Recopilar direcciones de correo electrónico" &rarr; <strong>No recopilar</strong></li>
                <li>&#x274C; <strong>Desactiva</strong> "Limitar a 1 respuesta" &rarr; déjala <strong>apagada</strong></li>
            </ul>
            <h4 style="color:#667eea;margin:10px 0 5px;font-size:13px;">&#x1F4DD; Texto múltiple con <code style="background:#e0e7ff;color:#3730a3;padding:2px 6px;border-radius:4px;font-size:12px;">&lt;gft&gt;</code></h4>
            <p style="font-size:13px;color:#374151;">Separa varias respuestas con <code style="background:#e0e7ff;color:#3730a3;padding:2px 6px;border-radius:4px;font-size:12px;">&lt;gft&gt;</code> y el bot elegirá una aleatoriamente en cada envío.</p>
            <ul style="padding-left:18px;margin:4px 0 10px;font-size:13px;color:#374151;">
                <li>Ejemplo: <code style="background:#e0e7ff;color:#3730a3;padding:2px 5px;border-radius:4px;font-size:11px;">Buena atención&lt;gft&gt;Excelente servicio&lt;gft&gt;Muy recomendado</code></li>
            </ul>
            <h4 style="color:#667eea;margin:10px 0 5px;font-size:13px;">&#x1F3AF; Probabilidades</h4>
            <ul style="padding-left:18px;margin:4px 0 10px;font-size:13px;color:#374151;">
                <li><strong>Nulo</strong> — Nunca se elige</li>
                <li><strong>Bajo</strong> — Poca frecuencia</li>
                <li><strong>Medio</strong> — Frecuencia normal</li>
                <li><strong>Alto</strong> — La gran mayoría de las veces</li>
            </ul>
            <h4 style="color:#667eea;margin:10px 0 5px;font-size:13px;">&#x26A1;&#xFE0F; Velocidades de envío</h4>
            <ul style="padding-left:18px;margin:4px 0 10px;font-size:13px;color:#374151;">
                <li>&#x26A1; <strong>Ultra Turbo (0.1s)</strong> — Velocidad extrema, solo formularios sin límite</li>
                <li>&#x1F7E2; <strong>Mega Turbo (0.3s)</strong> — Muy rápido, usar con precaución</li>
                <li>&#x1F7E2; <strong>Turbo (0.5s)</strong> — Máxima velocidad recomendada</li>
                <li>&#x1F535; <strong>Rápido (1s)</strong> — Buen equilibrio velocidad/estabilidad</li>
                <li>&#x1F7E1; <strong>Normal (2s)</strong> — Recomendado para la mayoría de casos</li>
                <li>&#x1F7E0; <strong>Seguro (3s)</strong> — Ideal para cantidades grandes</li>
                <li>&#x1F534; <strong>Cauteloso (5s)</strong> — Alta seguridad</li>
                <li>&#x1F7E3; <strong>Muy Cauteloso (10s)</strong> — Para formularios con restricciones</li>
                <li>&#x2B1B; <strong>Ultra Seguro (15s)</strong> — Mínimo riesgo de detección</li>
            </ul>
            <h4 style="color:#667eea;margin:10px 0 5px;font-size:13px;">&#x23F3; Cooldown y cantidad recomendada</h4>
            <p style="font-size:13px;color:#374151;">Al terminar, el botón se bloquea <strong>5 segundos</strong> automáticamente. Se pueden hacer hasta <strong>500 envíos</strong>, pero se recomienda en bloques de <strong>100 máximo</strong> con descanso entre sesiones.</p>
            <div style="background:#fef2f2;border:2px solid #fecaca;border-radius:8px;padding:12px;margin-top:14px;font-size:12px;color:#7f1d1d;line-height:1.6;">
                &#x26A0;&#xFE0F; <strong>Aviso:</strong> El vendedor de Zhero Bot no se hace responsable por bloqueos o restricciones generados por el uso de esta herramienta. El usuario asume toda la responsabilidad.
            </div>
        </div>

        <div class="form-group" style="margin-top:18px;">
            <label>URL del Google Form</label>
            <div class="input-group">
                <input type="text" id="formUrl" placeholder="https://docs.google.com/forms/d/e/...">
                <button class="btn-primary" id="analyzeBtn" onclick="analyzeForm()">Analizar</button>
            </div>
        </div>
        <div class="grid-2">
            <div class="form-group">
                <label>Numero de envios</label>
                <input type="number" id="submissions" value="10" min="1">
            </div>
            <div class="form-group">
                <label>&#x26A1;&#xFE0F; Velocidad de envío</label>
                <select id="delay">
                    <option value="0.1">&#x26A1; Ultra Turbo — 0.1s entre envíos</option>
                    <option value="0.3">&#x1F7E2; Mega Turbo — 0.3s entre envíos</option>
                    <option value="0.5">&#x1F7E2; Turbo — 0.5s entre envíos</option>
                    <option value="1">&#x1F535; Rápido — 1s entre envíos</option>
                    <option value="2" selected>&#x1F7E1; Normal — 2s entre envíos (recomendado)</option>
                    <option value="3">&#x1F7E0; Seguro — 3s entre envíos</option>
                    <option value="5">&#x1F534; Cauteloso — 5s entre envíos</option>
                    <option value="10">&#x1F7E3; Muy Cauteloso — 10s entre envíos</option>
                    <option value="15">&#x2B1B; Ultra Seguro — 15s entre envíos</option>
                </select>
            </div>
        </div>
    </div>

    <div class="card">
        <h2>&#x2699;&#xFE0F; Campos Detectados</h2>
        <div class="fields-container" id="fieldsContainer">
            <p style="text-align:center;color:#9ca3af;padding:40px;">
                No hay campos detectados. Analiza un formulario primero.
            </p>
        </div>
    </div>

    <div class="card">
        <h2>&#x1F680; Control de Envio</h2>
        <div class="stats">
            <div class="stat-box"><h3 id="statCurrent">0</h3><p>Actual</p></div>
            <div class="stat-box"><h3 id="statTotal">0</h3><p>Total</p></div>
            <div class="stat-box"><h3 id="statSuccess">0</h3><p>Exitosos</p></div>
            <div class="stat-box"><h3 id="statFailed">0</h3><p>Fallidos</p></div>
        </div>
        <div class="progress-bar">
            <div class="progress-fill" id="progressBar" style="width:0%">0%</div>
        </div>
        <div style="margin-top:20px;">
            <button class="btn-success" id="startBtn" onclick="startSubmissions()">&#x25B6;&#xFE0F; Iniciar Envios</button>
            <button class="btn-danger"  id="stopBtn"  onclick="stopSubmissions()" style="display:none;">&#x23F8;&#xFE0F; Detener</button>
        </div>
    </div>

    <div class="card">
        <h2>&#x1F4CB; Registro</h2>
        <div class="logs" id="logs"><div class="log-entry">Sistema iniciado...</div></div>
    </div>
</div>

<script>
let fields = [];
let statusInterval = null;
const SESSION_ID = Math.random().toString(36).substr(2,12) + Date.now().toString(36);

const TYPE_LABELS = {
    multiple_choice: 'Opcion multiple',
    checkboxes:      'Casillas',
    dropdown:        'Lista desplegable',
    linear_scale:    'Escala lineal',
    rating:          'Calificacion (estrellas)',
    grid:            'Cuadricula (radio)',
    grid_checkbox:   'Cuadricula (casillas)',
    short_text:      'Texto corto',
    paragraph:       'Parrafo',
    date:            'Fecha',
    time:            'Hora'
};

const TYPE_BADGES = {
    checkboxes:    'badge-checkbox',
    grid:          'badge-grid',
    grid_checkbox: 'badge-grid'
};

// ── Niveles de Probabilidad ──
const LEVELS = ['nulo', 'bajo', 'medio', 'alto'];
const LEVEL_LABELS = { nulo: 'Nulo', bajo: 'Bajo', medio: 'Medio', alto: 'Alto' };

function setLevel(fi, oi, lv, isGrid) {
    var key = isGrid ? 'col_probabilities' : 'probabilities';
    var opts = isGrid ? fields[fi].cols : fields[fi].options;
    if (!fields[fi][key] || fields[fi][key].length !== opts.length) {
        fields[fi][key] = opts.map(function() { return 'medio'; });
    }
    fields[fi][key][oi] = lv;
    renderFields();
}

function equalLevels(fi, isGrid) {
    var key  = isGrid ? 'col_probabilities' : 'probabilities';
    var opts = isGrid ? fields[fi].cols : fields[fi].options;
    fields[fi][key] = opts.map(function() { return 'medio'; });
    renderFields();
    addLog('Niveles igualados en "' + fields[fi].label + '"', 'success');
}

function randomLevels(fi, isGrid) {
    var key  = isGrid ? 'col_probabilities' : 'probabilities';
    var opts = isGrid ? fields[fi].cols : fields[fi].options;
    fields[fi][key] = opts.map(function() { return LEVELS[Math.floor(Math.random() * 4)]; });
    renderFields();
    addLog('Niveles aleatorios en "' + fields[fi].label + '"', 'success');
}

function renderProbSystem(field, fi, isGrid) {
    var key  = isGrid ? 'col_probabilities' : 'probabilities';
    var opts = isGrid ? (field.cols || []) : (field.options || []);
    if (!opts.length) return '';
    if (!field[key] || field[key].length !== opts.length) {
        field[key] = opts.map(function() { return 'medio'; });
    }
    var html = '<div class="prob-container">';
    html += '<div class="prob-header"><h4>&#x1F3AF; ' + (isGrid ? 'Probabilidad por columna' : 'Probabilidad') + '</h4></div>';
    for (var oi = 0; oi < opts.length; oi++) {
        var cur = field[key][oi] || 'medio';
        html += '<div class="prob-row">';
        html += '<div class="prob-label" title="' + opts[oi] + '">' + opts[oi] + '</div>';
        html += '<div class="prob-levels">';
        for (var li = 0; li < LEVELS.length; li++) {
            var lv = LEVELS[li];
            var active = cur === lv ? ' active' : '';
            html += '<button class="level-btn level-' + lv + active + '"';
            html += ' onclick="setLevel(' + fi + ',' + oi + ',\x27' + lv + '\x27,' + (isGrid ? 'true' : 'false') + ')">';
            html += LEVEL_LABELS[lv] + '</button>';
        }
        html += '</div></div>';
    }
    html += '<div class="prob-actions">';
    html += '<button class="btn-prob-action btn-igualar" onclick="equalLevels(' + fi + ',' + (isGrid ? 'true' : 'false') + ')">';
    html += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M3 10h18M3 14h18"/></svg> Igualar</button>';
    html += '<button class="btn-prob-action btn-aleatorio" onclick="randomLevels(' + fi + ',' + (isGrid ? 'true' : 'false') + ')">';
    html += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M16 3h5v5M4 20L21 3M21 16v5h-5M15 15l6 6M4 4l5 5"/></svg> Aleatorio</button>';
    html += '</div></div>';
    return html;
}
// ── Fin Niveles ──

function addLog(msg, type) {
    type = type || 'info';
    var logs = document.getElementById('logs');
    var t    = new Date().toLocaleTimeString();
    var div  = document.createElement('div');
    div.className = 'log-entry log-' + type;
    div.textContent = '[' + t + '] ' + msg;
    logs.appendChild(div);
    logs.scrollTop = logs.scrollHeight;
}

function toggleFieldMode(fi) {
    fields[fi].mode = fields[fi].mode === 'random' ? 'fixed' : 'random';
    if (fields[fi].mode === 'fixed') {
        var opts = fields[fi].options || fields[fi].cols || [];
        if (opts.length) fields[fi].value = opts[0];
    }
    renderFields();
    addLog('Campo "' + fields[fi].label + '" -> ' + fields[fi].mode, 'info');
}

function updateFieldValue(fi, val) {
    fields[fi].value = val;
}

function updateCheckboxCount(fi, n) {
    fields[fi].checkbox_count = parseInt(n) || 1;
}

function renderFieldContent(field, fi) {
    var html = '';

    if (field.type === 'short_text' || field.type === 'paragraph') {
        html += '<label>Valor a enviar</label>';
        html += '<small style="color:#6b7280;display:block;margin-bottom:8px;">Usa &lt;gft&gt; para multiples valores aleatorios</small>';
        if (field.type === 'paragraph') {
            html += '<textarea onchange="updateFieldValue(' + fi + ',this.value)">' + (field.value || '') + '</textarea>';
        } else {
            html += '<input type="text" value="' + (field.value || '') + '" onchange="updateFieldValue(' + fi + ',this.value)">';
        }
        return html;
    }

    if (field.type === 'date') {
        html += '<label>Fecha</label>';
        html += '<input type="date" value="' + (field.value || '') + '" onchange="updateFieldValue(' + fi + ',this.value)">';
        return html;
    }

    if (field.type === 'time') {
        html += '<label>Hora</label>';
        html += '<input type="time" value="' + (field.value || '') + '" onchange="updateFieldValue(' + fi + ',this.value)">';
        return html;
    }

    if (field.type === 'checkboxes') {
        if (field.mode === 'random') {
            html += '<div class="checkbox-qty"><label>Marcar simultaneamente:</label>';
            html += '<input type="number" value="' + (field.checkbox_count || 1) + '" min="1" max="' + (field.options ? field.options.length : 1) + '"';
            html += ' onchange="updateCheckboxCount(' + fi + ',this.value)"></div>';
            html += renderProbSystem(field, fi, false);
        } else {
            html += '<label>Seleccionar opciones fijas</label>';
            html += '<select multiple size="' + Math.min(5, (field.options || []).length) + '"';
            html += ' onchange="updateFieldValue(' + fi + ',Array.from(this.selectedOptions).map(function(o){return o.value;}))">';
            (field.options || []).forEach(function(opt) {
                var sel = Array.isArray(field.value) ? field.value.indexOf(opt) >= 0 : field.value === opt;
                html += '<option value="' + opt + '"' + (sel ? ' selected' : '') + '>' + opt + '</option>';
            });
            html += '</select>';
        }
        return html;
    }

    if (field.type === 'grid' || field.type === 'grid_checkbox') {
        if (field.rows && field.rows.length) {
            html += '<div class="grid-preview"><table class="grid-table"><thead><tr><th>Fila</th>';
            (field.cols || []).forEach(function(c) { html += '<th>' + c + '</th>'; });
            html += '</tr></thead><tbody>';
            field.rows.forEach(function(row, ri) {
                html += '<tr><td><strong>' + row + '</strong></td>';
                (field.cols || []).forEach(function(c, ci) {
                    var t = field.type === 'grid_checkbox' ? 'checkbox' : 'radio';
                    html += '<td><input type="' + t + '" name="g' + fi + '_' + ri + '"' + (ci === 0 ? ' checked' : '') + '></td>';
                });
                html += '</tr>';
            });
            html += '</tbody></table></div>';
        }
        if (field.mode === 'random') {
            html += renderProbSystem(field, fi, true);
        } else {
            html += '<label style="margin-top:10px;display:block;">Columna fija para todas las filas</label>';
            html += '<select onchange="updateFieldValue(' + fi + ',this.value)">';
            (field.cols || []).forEach(function(c) {
                html += '<option value="' + c + '"' + (field.value === c ? ' selected' : '') + '>' + c + '</option>';
            });
            html += '</select>';
        }
        return html;
    }

    // multiple_choice, dropdown, linear_scale, rating
    if (field.type === 'rating') {
        if (field.mode === 'fixed') {
            html += '<label>Calificacion fija (estrellas)</label>';
            html += '<select onchange="updateFieldValue(' + fi + ',this.value)"><option value="">-- Seleccionar --</option>';
            ['1','2','3','4','5'].forEach(function(opt) {
                var stars = '⭐'.repeat(parseInt(opt));
                html += '<option value="' + opt + '"' + (field.value === opt ? ' selected' : '') + '>' + stars + ' (' + opt + ')</option>';
            });
            html += '</select>';
        } else {
            html += renderProbSystem(field, fi, false);
        }
        return html;
    }

    // multiple_choice, dropdown, linear_scale
    if (field.options && field.options.length) {
        if (field.mode === 'fixed') {
            html += '<label>Opcion fija</label>';
            html += '<select onchange="updateFieldValue(' + fi + ',this.value)"><option value="">-- Seleccionar --</option>';
            field.options.forEach(function(opt) {
                html += '<option value="' + opt + '"' + (field.value === opt ? ' selected' : '') + '>' + opt + '</option>';
            });
            html += '</select>';
        } else {
            html += renderProbSystem(field, fi, false);
        }
        return html;
    }

    return '<p style="color:#9ca3af;">Sin configuracion</p>';
}

function renderFields() {
    var container = document.getElementById('fieldsContainer');
    if (!fields.length) {
        container.innerHTML = '<p style="text-align:center;color:#9ca3af;padding:40px;">Sin campos</p>';
        return;
    }
    var hasToggle = function(f) {
        return (f.options && f.options.length) || f.type === 'checkboxes' || f.type === 'grid' || f.type === 'grid_checkbox';
    };
    container.innerHTML = fields.map(function(field, fi) {
        var typeLabel = TYPE_LABELS[field.type] || field.type;
        var badgeClass = TYPE_BADGES[field.type] || '';
        var modeClass = field.mode === 'random' ? 'btn-mode-random' : 'btn-mode-fixed';
        var modeIcon  = field.mode === 'random' ? '&#x1F3B2; Aleatorio' : '&#x1F4CC; Fijo';

        var html = '<div class="field-item">';
        html += '<div class="field-header">';
        html += '<div class="field-title">' + field.label + '</div>';
        html += '<span class="field-type-badge ' + badgeClass + '">' + typeLabel + '</span>';
        if (hasToggle(field)) {
            html += '<button class="btn-mode ' + modeClass + '" onclick="toggleFieldMode(' + fi + ')">' + modeIcon + '</button>';
        }
        html += '</div>';
        html += '<div>' + renderFieldContent(field, fi) + '</div>';
        html += '</div>';
        return html;
    }).join('');
}

async function analyzeForm() {
    var url = document.getElementById('formUrl').value.trim();
    if (!url.includes('docs.google.com/forms')) {
        addLog('URL invalida', 'error'); return;
    }
    addLog('Analizando formulario...', 'info');
    document.getElementById('analyzeBtn').disabled = true;
    try {
        var resp = await fetch('/analyze', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({url: url, session_id: SESSION_ID})
        });
        var data = await resp.json();
        if (data.success) {
            fields = data.fields;
            // Normalizar probabilidades: el backend devuelve números (20.0),
            // el frontend usa strings ('nulo','bajo','medio','alto').
            // Si son números → convertir todo a 'medio' por defecto.
            fields.forEach(function(f) {
                function normProbs(arr) {
                    if (!arr || !arr.length) return arr;
                    var isNumeric = arr.some(function(p) { return typeof p === 'number'; });
                    if (isNumeric) return arr.map(function() { return 'medio'; });
                    return arr;
                }
                f.probabilities     = normProbs(f.probabilities);
                f.col_probabilities = normProbs(f.col_probabilities);
            });
            addLog('OK: ' + data.count + ' campos detectados', 'success');
            var types = {};
            fields.forEach(function(f) { types[f.type] = (types[f.type] || 0) + 1; });
            Object.keys(types).forEach(function(t) {
                addLog('  -> ' + types[t] + 'x ' + (TYPE_LABELS[t] || t), 'info');
            });
            renderFields();
        } else {
            addLog('Error: ' + data.error, 'error');
        }
    } catch(e) {
        addLog('Error: ' + e.message, 'error');
    }
    document.getElementById('analyzeBtn').disabled = false;
}

async function startSubmissions() {
    if (!fields.length) { addLog('Analiza un formulario primero', 'error'); return; }
    var url   = document.getElementById('formUrl').value.trim();
    var count = parseInt(document.getElementById('submissions').value);
    var delay = parseFloat(document.getElementById('delay').value);
    document.getElementById('startBtn').style.display = 'none';
    document.getElementById('stopBtn').style.display  = 'inline-block';
    addLog('Iniciando ' + count + ' envios...', 'info');
    try {
        var resp = await fetch('/start', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({url: url, fields: fields, count: count, delay: delay, session_id: SESSION_ID})
        });
        var data = await resp.json();
        if (data.success) { addLog('Iniciado', 'success'); startPolling(); }
        else { addLog('Error: ' + data.error, 'error'); resetButtons(); }
    } catch(e) { addLog('Error: ' + e.message, 'error'); resetButtons(); }
}

async function stopSubmissions() {
    addLog('Deteniendo...', 'info');
    await fetch('/stop', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({session_id: SESSION_ID})});
    stopPolling(); resetButtons();
}

function startPolling() {
    statusInterval = setInterval(async function() {
        var resp   = await fetch('/status?session_id=' + SESSION_ID);
        var status = await resp.json();
        document.getElementById('statCurrent').textContent = status.current;
        document.getElementById('statTotal').textContent   = status.total;
        document.getElementById('statSuccess').textContent = status.successful;
        document.getElementById('statFailed').textContent  = status.failed;
        var pct = status.total > 0 ? (status.current / status.total) * 100 : 0;
        document.getElementById('progressBar').style.width = pct + '%';
        document.getElementById('progressBar').textContent = Math.round(pct) + '%';
        if (!status.is_sending) {
            stopPolling(); resetButtons();
            if (status.current > 0) addLog('Completado: ' + status.successful + ' OK, ' + status.failed + ' fallidos', 'success');
        }
    }, 500);
}

function stopPolling() { if (statusInterval) { clearInterval(statusInterval); statusInterval = null; } }

function toggleHelp() {
    var box = document.getElementById('helpBox');
    box.style.display = box.style.display === 'block' ? 'none' : 'block';
}

function resetButtons() {
    var startBtn = document.getElementById('startBtn');
    document.getElementById('stopBtn').style.display = 'none';
    var secs = 5;
    startBtn.style.display = 'inline-block';
    startBtn.disabled = true;
    startBtn.style.background = '#9ca3af';
    startBtn.style.cursor = 'not-allowed';
    startBtn.textContent = '⏳ Espera ' + secs + 's...';
    var cd = setInterval(function() {
        secs--;
        if (secs > 0) {
            startBtn.textContent = '⏳ Espera ' + secs + 's...';
        } else {
            clearInterval(cd);
            startBtn.disabled = false;
            startBtn.style.background = '';
            startBtn.style.cursor = '';
            startBtn.innerHTML = '&#x25B6;&#xFE0F; Iniciar Envios';
        }
    }, 1000);
}
</script>

</div><!-- /mainContent -->

<script>
/* ══════════════════════════════════════
   LÓGICA DE PANTALLA DE CARGA
══════════════════════════════════════ */
(function() {
    var bar      = document.getElementById('splashBar');
    var statusEl = document.getElementById('splashStatus');
    var splash   = document.getElementById('splashScreen');
    var main     = document.getElementById('mainContent');
    var bg       = document.getElementById('splashBg');

    // Generar partículas flotantes
    for (var i = 0; i < 18; i++) {
        var p = document.createElement('div');
        p.className = 'particle';
        var size = Math.random() * 60 + 20;
        p.style.cssText = [
            'width:'  + size + 'px',
            'height:' + size + 'px',
            'left:'   + Math.random() * 100 + '%',
            'animation-duration:' + (Math.random() * 8 + 6) + 's',
            'animation-delay:'    + (Math.random() * 5) + 's'
        ].join(';');
        bg.appendChild(p);
    }

    // Pasos de carga con mensajes
    var steps = [
        { pct: 15, msg: 'Cargando módulos...'       },
        { pct: 35, msg: 'Conectando con el servidor...' },
        { pct: 55, msg: 'Preparando interfaz...'    },
        { pct: 75, msg: 'Verificando configuración...' },
        { pct: 90, msg: 'Casi listo...'              },
        { pct: 100, msg: '¡Bienvenido/a!'            }
    ];

    var idx = 0;
    var delays = [800, 900, 850, 900, 800, 900];

    function nextStep() {
        if (idx >= steps.length) {
            // Ocultar splash y mostrar app
            setTimeout(function() {
                splash.classList.add('hidden');
                main.style.display = 'block';
                // Forzar reflow para la animación de entrada
                main.style.opacity = '0';
                main.style.transition = 'opacity 0.6s ease';
                setTimeout(function() { main.style.opacity = '1'; }, 50);
            }, 300);
            return;
        }
        bar.style.width = steps[idx].pct + '%';
        statusEl.textContent = steps[idx].msg;
        idx++;
        setTimeout(nextStep, delays[idx - 1] || 400);
    }

    // Arrancar después de un pequeño delay inicial
    setTimeout(nextStep, 400);
})();
</script>

</body>
</html>

"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/analyze', methods=['POST'])
def analyze():
    data = request.json
    url  = data.get('url')
    sid  = data.get('session_id', 'default')
    b    = GoogleFormBot(url)
    result = b.analyze_form()
    if result.get('success'):
        sessions[sid] = b
    return jsonify(result)

@app.route('/start', methods=['POST'])
def start():
    data   = request.json
    sid    = data.get('session_id', 'default')
    st     = statuses.get(sid, {})
    if st.get('is_sending'):
        return jsonify({'success': False, 'error': 'Ya tienes un proceso en ejecucion'})
    url    = data.get('url')
    fields = data.get('fields', [])
    count  = data.get('count', 10)
    delay  = data.get('delay', 2.0)
    if sid not in sessions:
        sessions[sid] = GoogleFormBot(url)
    sessions[sid].fields = fields
    statuses[sid] = {
        'is_sending': True, 'current': 0, 'total': count,
        'successful': 0, 'failed': 0, 'should_stop': False
    }
    thread = threading.Thread(target=submit_worker, args=(sid, count, delay))
    thread.daemon = True
    thread.start()
    return jsonify({'success': True})

def submit_worker(sid, count, delay):
    for i in range(count):
        if statuses[sid]['should_stop']:
            break
        success, _ = sessions[sid].submit_form()
        statuses[sid]['current'] = i + 1
        if success:
            statuses[sid]['successful'] += 1
        else:
            statuses[sid]['failed'] += 1
        if i < count - 1:
            time.sleep(delay)
    statuses[sid]['is_sending'] = False

@app.route('/stop', methods=['POST'])
def stop():
    data = request.json
    sid  = data.get('session_id', 'default') if request.json else 'default'
    if sid in statuses:
        statuses[sid]['should_stop'] = True
        statuses[sid]['is_sending']  = False
    return jsonify({'success': True})

@app.route('/status', methods=['GET'])
def status():
    sid = request.args.get('session_id', 'default')
    return jsonify(statuses.get(sid, {
        'is_sending': False, 'current': 0, 'total': 0,
        'successful': 0, 'failed': 0, 'should_stop': False
    }))

if __name__ == '__main__':
    print("="*60)
    print("Bot de Google Forms - Versión Mejorada con Probabilidades")
    print("="*60)
    print("\n✓ Opción múltiple")
    print("✓ Casillas de verificación")
    print("✓ Lista desplegable")
    print("✓ Cuadrícula (MEJORADO)")
    print("✓ Escala lineal")
    print("✓ Texto y párrafo")
    print("✓ Fecha y hora")
    print("✓ Sistema de Probabilidades (NUEVO)")
    print("\nServidor: http://localhost:5000")
    print("\nPresiona Ctrl+C para detener\n")
    print("="*60)
    
    app.run(debug=True, host='0.0.0.0', port=5000)
