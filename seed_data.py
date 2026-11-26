# Catálogo de exemplo do Marketplace B2B. Edite aqui e rode `flask --app app init-db` em um banco novo.
CATEGORIAS = ["Usinagem", "Serras e Lâminas", "Solda", "EPIs", "Abrasivos", "Ferramentas e Medição"]

# (nome, preço, índice da categoria em CATEGORIAS)
PRODUTOS = [
    ("Fresa de Topo Metal Duro 10 mm 4 Cortes", "189.90", 0),
    ("Jogo de Brocas HSS 19 Peças", "149.90", 0),
    ("Inserto de Torneamento CNMG 120408 (caixa 10 un)", "259.00", 0),
    ("Serra Fita Bimetal 27 x 0,9 mm", "129.90", 1),
    ("Kit Serra Copo Bimetálica 8 Peças", "219.90", 1),
    ("Lâmina de Serra Sabre para Metal (5 un)", "59.90", 1),
    ("Eletrodo E6013 2,5 mm (5 kg)", "74.90", 2),
    ("Máscara de Solda com Escurecimento Automático", "289.90", 2),
    ("Arame MIG ER70S-6 0,8 mm (15 kg)", "369.00", 2),
    ("Luva de Raspa para Soldador", "34.90", 3),
    ("Óculos de Proteção Incolor", "12.90", 3),
    ("Protetor Auricular Tipo Concha", "49.90", 3),
    ("Disco de Corte Inox 4.1/2 pol (caixa 25 un)", "99.90", 4),
    ("Disco de Desbaste 7 pol", "18.90", 4),
    ("Disco Flap 4.1/2 pol Grão 80", "14.90", 4),
    ("Paquímetro Digital 150 mm", "179.90", 5),
    ("Micrômetro Externo 0-25 mm", "239.90", 5),
    ("Torno de Bancada nº 5", "449.00", 5),
]

# Regras de atacado de exemplo, pelo nome do produto (o resto vende a partir de 1 unidade).
# faixas: (a partir de N unidades, preço de cada uma); fraciona: % de acréscimo para vender fora da embalagem
REGRAS_ATACADO = {
    "Disco de Desbaste 7 pol": {"faixas": [(10, "16.90"), (50, "14.90")]},
    "Disco Flap 4.1/2 pol Grão 80": {"multiplo": 10, "faixas": [(50, "12.90")], "fraciona": "10"},
    "Óculos de Proteção Incolor": {"minimo": 12, "multiplo": 12, "faixas": [(120, "10.90")]},
    "Luva de Raspa para Soldador": {"faixas": [(12, "31.90"), (60, "28.90")]},
}

# Códigos (SKU) dos produtos de exemplo, usados no pedido rápido
CODIGOS = {
    "Fresa de Topo Metal Duro 10 mm 4 Cortes": "FRESA-10", "Jogo de Brocas HSS 19 Peças": "BROCA-HSS19",
    "Inserto de Torneamento CNMG 120408 (caixa 10 un)": "INS-CNMG", "Serra Fita Bimetal 27 x 0,9 mm": "SERRA-FITA27",
    "Kit Serra Copo Bimetálica 8 Peças": "SERRA-COPO8", "Lâmina de Serra Sabre para Metal (5 un)": "LAMINA-SABRE",
    "Eletrodo E6013 2,5 mm (5 kg)": "ELET-6013", "Máscara de Solda com Escurecimento Automático": "MASC-AUTO",
    "Arame MIG ER70S-6 0,8 mm (15 kg)": "ARAME-MIG08", "Luva de Raspa para Soldador": "LUVA-RASPA",
    "Óculos de Proteção Incolor": "OCULOS-INC", "Protetor Auricular Tipo Concha": "PROT-CONCHA",
    "Disco de Corte Inox 4.1/2 pol (caixa 25 un)": "DISCO-INOX", "Disco de Desbaste 7 pol": "DISCO-7",
    "Disco Flap 4.1/2 pol Grão 80": "DISCO-FLAP80", "Paquímetro Digital 150 mm": "PAQ-150",
    "Micrômetro Externo 0-25 mm": "MICRO-25", "Torno de Bancada nº 5": "TORNO-5",
}
