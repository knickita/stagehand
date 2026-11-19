import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Polygon, Patch

root = Path('D:/Repositories/stagehand/Stagehand')
scene = json.loads((root / 'tmp/stage_perimeter_scene.json').read_text())
fig, ax = plt.subplots(figsize=(13.5, 8.5), dpi=160)
fig.patch.set_facecolor('#ffffff')
colors = {0.485: '#e9f0f7', 1.0: '#bcd3e8'}
for obj in scene['objects']:
    if 'pedana' not in obj['name']:
        continue
    pts = obj['bounds']
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    z = round(max(p[2] for p in pts), 3)
    ax.add_patch(Rectangle((x0, y0), x1-x0, y1-y0, facecolor=colors[z], edgecolor='#9eafbd', linewidth=.55))
outline = [(-4, 0), (20, 0), (20, 4), (16, 4), (16, 12), (0, 12), (0, 4), (-4, 4)]
ax.add_patch(Polygon(outline, closed=True, fill=False, edgecolor='#18364a', linewidth=2))
ax.plot([0, 16], [8, 8], color='#b76d18', linewidth=2, linestyle='--')

blue = '#155ba6'
def label(x, y, text, rotation=0, color=blue, size=11):
    ax.text(x, y, text, ha='center', va='center', rotation=rotation, color=color, fontsize=size,
            bbox=dict(facecolor='white', edgecolor='none', pad=2))
def hdim(x0, x1, y, source_y, text):
    for x in (x0, x1):
        ax.plot([x, x], [source_y, y], color=blue, linewidth=.7)
    ax.annotate('', (x0, y), (x1, y), arrowprops=dict(arrowstyle='<->', color=blue, lw=1))
    label((x0+x1)/2, y, text)
def vdim(y0, y1, x, source_x, text):
    for y in (y0, y1):
        ax.plot([source_x, x], [y, y], color=blue, linewidth=.7)
    ax.annotate('', (x, y0), (x, y1), arrowprops=dict(arrowstyle='<->', color=blue, lw=1))
    label(x, (y0+y1)/2, text, rotation=90)

hdim(-4, 0, -1.0, 0, '4,00')
hdim(0, 16, -1.0, 0, '16,00')
hdim(16, 20, -1.0, 0, '4,00')
hdim(-4, 20, -2.2, 0, '24,00 m - ingombro')
hdim(0, 16, 13.1, 12, '16,00')
hdim(-4, 0, 4.8, 4, '4,00')
hdim(16, 20, 4.8, 4, '4,00')
vdim(0, 4, -5.1, -4, '4,00')
vdim(4, 12, -5.1, 0, '8,00')
vdim(0, 12, 22.4, 20, '12,00 m - ingombro')
vdim(0, 8, 21.0, 16, '8,00')
vdim(8, 12, 21.0, 16, '4,00')
label(8, 4.9, 'Calpestio +0,485 m', color='#18364a', size=12)
label(8, 10, 'Calpestio +1,000 m', color='#18364a', size=12)
label(8, 8, 'Cambio di livello: +0,515 m', color='#99530c', size=10)
ax.plot(-4, 0, marker='o', color='#18364a', markersize=5)
ax.text(-3.8, .35, 'A', color='#18364a', fontsize=11, fontweight='bold')
ax.set_title('Proposta di quotatura - geometria del progetto allegato', loc='left', fontsize=17, fontweight='bold', color='#18364a', pad=20)
fig.text(.125, .07, 'Pianta ricavata dalle pedane 2 x 1 m. Misure in metri. A = riferimento proposto per il tracciamento.', fontsize=10, color='#526573')
fig.text(.125, .045, 'Schema di discussione: lato pubblico e orientamento di montaggio da definire.', fontsize=10, color='#526573')
ax.set_aspect('equal')
ax.set_xlim(-7, 24)
ax.set_ylim(-3, 14)
ax.axis('off')
fig.subplots_adjust(bottom=.13, top=.9, left=.09, right=.96)
out = root / 'output/stage_perimeter_proposal.png'
fig.savefig(out, facecolor='white')
print(out)
