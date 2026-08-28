-- Migration: separate product catalog for offer-enquiry system
-- Run once in Supabase SQL Editor.

-- ── 1. Create catalog_products table ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS catalog_products (
    id          SERIAL PRIMARY KEY,
    model_name  VARCHAR(500) NOT NULL,
    code        VARCHAR(200),
    category    VARCHAR(200),
    definition  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── 2. Specifications junction (mirrors product_specifications) ──────────────
CREATE TABLE IF NOT EXISTS catalog_product_specifications (
    id                   SERIAL PRIMARY KEY,
    catalog_product_id   INTEGER NOT NULL REFERENCES catalog_products(id) ON DELETE CASCADE,
    specification_id     INTEGER NOT NULL REFERENCES specifications(id)   ON DELETE CASCADE,
    display_order        INTEGER NOT NULL DEFAULT 0,
    UNIQUE (catalog_product_id, specification_id)
);

-- ── 3. Re-point offer_items / enquiry_items FKs ─────────────────────────────
-- Drop old FK constraints (referenced the BOQ products table)
ALTER TABLE offer_items    DROP CONSTRAINT IF EXISTS offer_items_product_id_fkey;
ALTER TABLE enquiry_items  DROP CONSTRAINT IF EXISTS enquiry_items_product_id_fkey;

-- Make product_id nullable (the column may carry a NOT NULL constraint in the DB)
ALTER TABLE offer_items   ALTER COLUMN product_id DROP NOT NULL;
ALTER TABLE enquiry_items ALTER COLUMN product_id DROP NOT NULL;

-- NULL out stale product_id values — old IDs referenced the products table
-- and will not match catalog_products IDs (the description text is preserved).
UPDATE offer_items   SET product_id = NULL WHERE product_id IS NOT NULL;
UPDATE enquiry_items SET product_id = NULL WHERE product_id IS NOT NULL;

-- Add new FK constraints pointing to catalog_products
ALTER TABLE offer_items   ADD CONSTRAINT offer_items_catalog_product_fkey
    FOREIGN KEY (product_id) REFERENCES catalog_products(id) ON DELETE SET NULL;
ALTER TABLE enquiry_items ADD CONSTRAINT enquiry_items_catalog_product_fkey
    FOREIGN KEY (product_id) REFERENCES catalog_products(id) ON DELETE SET NULL;

-- ── 4. Seed data from product-catalog.csv (73 products) ─────────────────────
INSERT INTO catalog_products (model_name, code, category, definition) VALUES
('E-Safe Fibre Glass Single Step Ladder ES-SSL-H*', 'ES-SSL-H* (Height)', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Heavy Duty Self Support ''A'' type single step ladder Model ES-SSL- H* (height) hav...'),
('E-Safe Fibre Glass Double Step Ladder ES-DSL-H*', 'ES-DSL-H* (Height)', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Heavy Duty Self Support ''A'' type Double step ladder Model ES-DSL- H* (height) hav...'),
('E-Safe Fibre Glass Trestel Step Ladder ES-TSL-H*', 'ES-TSL-H* (Height)', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Heavy Duty Self Support ''A'' type Trestle step ladder Model ES-TSL- H* (Height) ha...'),
('E-Safe Fibre Glass Platform Step Ladder ES-PSL-H*', 'ES-PSL-H* (Height)', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Heavy Duty self support type Platform step ladder Model ES-PSL - H* (Height) havi...'),
('E-Safe Fibre Glass Double Side Platform Step Ladder ES-DPSL-H*', 'ES-DPSL-H* (Height)', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Heavy Duty self support Double Side Platform step ladder Model ES-DPSL - H* (Heig...'),
('E-Safe Fibre Glass Pull Stool Ladder ES-PS-H*', 'ES-PS-H* (Height)', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Standard duty ''A'' type self-supported Pull Stool Ladder Model ES-PS - H* (Height)...'),
('E-Safe Fibre Glass Heavy Duty Pull Stool Ladder ES-PSHD-H*', 'ES-PSHD-H* (Height)', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Heavy duty ''A'' type self-supported Pull Stool Ladder Model ES-PSHD - H* (Height) ...'),
('E-Safe Fibre Glass Mobile Maintenance Platform Trolley Ladder ES-MT-H*', 'ES-MT-H* (Height)', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Self Support Mobile Maintenance Platform Trolley ladder Model ES-MT - H*(Height) ...'),
('E-Safe Fibre Glass Heavy Duty Mobile Maintenance Platform Trolley Ladder ES-MTHD-H*', 'ES-MTHD-H*', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Heavy Duty Self Support Mobile Maintenance Platform Trolley ladder Model ES-MTHD ...'),
('E-Safe Fibre Glass Mobile Platform Trolley Ladder ES-PTL-H*', 'ES-PTL-H*', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Self Support Mobile Platform Trolley ladder Model ES-PTL - H* (Height) having sid...'),
('E-Safe Fibre Glass Heavy Duty Mobile Platform Trolley Ladder ES-PTLHD-H*', 'ES-PTLHD-H*', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Heavy Duty Self Support Mobile Platform Trolley ladder Model ES-PTLHD - H* (Heigh...'),
('E-Safe Fibre Glass Heavy Duty Wall Support Extension Ladder ES-EL-H*', 'ES-EL-H*', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Heavy Duty Wall support Sway Proof Extension Ladder Model ES-EL-H* (Height) havin...'),
('E-Safe Fibre Glass Heavy Duty Wall Support Single Ladder ES-SL-H*', 'ES-SL-H*', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fiber Glass Heavy duty wall support type single ladder Model ES-SL-H* (Height) having side ru...'),
('E-Safe Fibre Glass Heavy Duty Self Support Extension Ladder ES-SEL-H*', 'ES-ES-SEL-H*', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Self Support Extendable Ladder Model ES-SEL-H* (Height) having side runner of FRP...'),
('E-Safe Fibre Glass Heavy Duty Tiltable Tower Extension Ladder ES-TTL-H*', 'ES-TTL-H*', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Heavy Duty Self-supported Tiltable Tower Extension Ladder Model ES-TTL-H* (Height...'),
('E-Safe Fibre Glass Heavy Duty Scaffold Tower ES-SC-H*', 'ES-SC-H*', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Heavy Duty Scaffold Tower with FRP sections and platforms.'),
('E-Safe Fibre Glass Skeleton Ladder ES-SK-H*', 'ES-SK-H*', 'E-SAFE FIBRE GLASS LADDERS', 'E-Safe Fiber Glass Heavy Duty Pole mount skeleton ladder Model ES-SK-H* (Height) pole mounting brack...'),
('E-Safe Fibre Glass Step Stand Ladder ES-SS-H*', 'ES-SS-H*', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Heavy-Duty Non-Conductive Step Stands Model ES-SS - H* (Height) Provide Convenien...'),
('E-Safe Fibre Glass Panel Room Platform ES-PRP-H*', 'ES-PRP-H*', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Manufactures special purpose safety platforms to be used in Panel Rooms. These panel room saf...'),
('E-Safe Fibre Glass Isolation / Rescue Hook ES-IS-kV*', 'ES-IS-kV*', 'E-Safe Fibre Glass Operating Discharge Rods', 'E-Safe Fibre Glass Isolation / Rescue Stick Model ES-IS-kV* (kV Rating) is an invaluable tool for a...'),
('E-Safe Fibre Glass Operating Discharge Rod', 'E-Safe Fibre Glass Operating Discharge Rod', 'E-Safe Fibre Glass Operating Discharge Rods', 'E-Safe Fibre Glass Electrical earthing discharge rods are specialised safety devices used to safely ...'),
('E-Safe FRP Telescopic Ladder ES-TL-H*', 'ES-TL-H*', 'E-SAFE FIBRE GLASS LADDERS', 'E-Safe FRP telescopic ladders are constructed from FRP, offering high insulation against electricity...'),
('E-Safe FRP Work Bench ES-WB', 'ES-WB', 'E-Safe FRP Other Products', 'E-Safe FRP work benches are robust work surfaces constructed from Fiber Reinforced Plastic (FRP), de...'),
('E-Safe Aluminium Single Step Ladder ES-ASSL-H*', 'ES-ASSL-H*', 'E-Safe Aluminium Ladders', 'E-SAFE Aluminium Heavy Duty Self Support ''A'' type single step ladder Model ES-ASSL- H* (height) havi...'),
('E-Safe Aluminium Double Step Ladder ES-ADSL-H*', 'ES-ADSL-H*', 'E-Safe Aluminium Ladders', 'E-SAFE Aluminium Heavy Duty Self Support ''A'' type double step ladder Model ES-ADSL- H* (height) havi...'),
('E-Safe Aluminium Wall Support Single Ladder ES-ASL-H*', 'ES-ASL-H*', 'E-Safe Aluminium Ladders', 'E-SAFE Aluminium Heavy duty wall support type single ladder Model ES-ASL-H* (Height) having side run...'),
('E-Safe Earthing Grounding Set (Short Link) ES-EGSL', 'ES-EGSL', 'E-Safe Fibre Glass Operating Discharge Rods', 'E-SAFE Earthing Grounding Short Link Set. Provided with Special Purpose clamps for Connecting Phase,...'),
('E-Safe Crowbar ES-Crowbar', 'ES-Crowbar', 'E-Safe FRP Other Products', 'E-Safe Insulated crowbar is a type of crowbar made from Fiber Reinforced Plastic and Steel, offerin...'),
('E-Safe Fibre Glass Non Conductive Wire Lifter ES-WL', 'ES-WL', 'E-Safe FRP Other Products', 'E-Safe Fibre Glass telescopically operated Non Conductive Wire Lifter is designed to safely lift the...'),
('E-Safe Fibre Glass Cage Ladder ES-CL-H*', 'ES-CL-H*', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Heavy Duty Cage ladders are used in a wide range of industries. Our Cage Ladders ...'),
('E-Safe FRP Tree Pruner', 'ES-TP', 'E-Safe FRP Other Products', 'E-Safe manufactures Tree pruners for cutting branches up to 1" thickness which are near electrical l...'),
('E-Safe High Voltage Non Contact Sensor', 'ES-HV', 'E-Safe FRP Other Products', 'E-Safe High Voltage Non Contact Sensor is a safety device designed to Detect any AC voltage and Indu...'),
('E-Safe Insulating Platform - Indoor', 'ES-IPP', 'E-Safe Plastic Molded Products', 'E-Safe Insulating Platforms are Best Safety Platforms for the safety of working professionals at Ele...'),
('E-Safe Insulating Platform - Outdoor', 'ES-IPF', 'E-Safe Plastic Molded Products', 'E-Safe Insulating Platforms are Best Safety Platforms for the safety of working professionals at Ele...'),
('E-Safe Barricade', 'ES-Barricade', 'E-Safe FRP Other Products', 'E-Safe Fibre Glass barricades are light weight barricades best suited for Corrosive, high temperatur...'),
('ES-FL-H', 'ES-FL-H', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Standard Duty sway proof Folding ladder Height having side runners of fibre glas...'),
('ES-PFTL-H', 'E SAFE FRP Platform Foldable Trolley Ladder', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Heavy Duty Self Support Mobile Maintenance Platform Trolley ladder Model ES-MTHD ...'),
('ES-FPTL-H', 'E SAFE FRP Foldable Platform Trolley Ladder', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Heavy Duty Self Support Mobile Maintenance Foldable Platform Trolley ladder Model...'),
('Model ES-Trolley Ladder', 'Customised Trolley Ladder', 'E-SAFE FIBRE GLASS LADDERS', 'E SAFE Fibre Glass Heavy Duty Platform Trolley Ladder of height having working platform. Fibre Glas...'),
('Model ES-MH-H', 'E SAFE FRP Man Hole Ladder', 'E-SAFE FIBRE GLASS LADDERS', 'E-SAFE Fibre Glass Heavy Duty sway proof Manhole ladder of Height having side runners of fibre glass...'),
('E-Safe SAFEHAND Push Pull Rod ES-SPPR', 'Model ES-SPPR', 'E-Safe Fibre Glass Hand Safety Tools', 'The E-Safe SAFEHAND Push Pull hand safety tool helps avoid hand injuries while working with suspende...'),
('ES-FRP Hand Rail', 'ES-FRP Hand Rail', 'E-Safe FRP Other Products', 'E SAFE Fiber Glass Hand Railing'),
('E-Safe Shoring Box', 'E-Safe Trench Shoring Box', 'E-Safe FRP Other Products', 'E-safe pultrusion expertise and global manufacturing capabilities have enabled it to realise its new...'),
('FRP Telescopic Hot Line Stick', 'FRP Telescopic Hot Line Stick', 'E-Safe FRP Other Products', 'E-Safe fibre glass heavy duty highly insulated FRP Telescopic Hot Line Stick for 25kv traction line ...'),
('E-Safe Aluminium Trestle Step Ladder ES-ATSL-H*', 'ES-ATSL-H', 'E-Safe Aluminium Ladders', 'E-SAFE Aluminium Heavy Duty Self Support ''A'' type Trestle step ladder Model ES-ATSL- H* (height) hav...'),
('Accessories of Discharge Rod', 'Copper Cable', 'E-Safe Fibre Glass Operating Discharge Rods', 'Accessories of Discharge Rod'),
('E-SAFE Trefoil Clamp', 'Trefoil Clamp', 'E-Safe Plastic Molded Products', 'E-SAFE Heavy-duty Trefoil Clamp suitable for OD Cable size'),
('Rope Ladder', 'PP Rope Ladder', 'E-Safe FRP Other Products', 'PP Rope Ladder of total Length'),
('FRP Fencing', 'E SAFE FRP Fencing', 'E-Safe FRP Other Products', 'E SAFE FRP Fencing'),
('E SAFE PLATFORM ALUMINUM LADDER', 'ES-APSL-H', 'E-Safe Aluminium Ladders', 'E-SAFE Aluminum Self-Support type Platform step ladder of H working platform (Standing) height havin...'),
('FRP Barrier', 'ES FRP Barrier', 'E-Safe FRP Other Products', 'E SAFE FRP Barrier With Top Height and Length'),
('FRP SAFEHAND TOOL', 'FRP SAFEHAND TOOL', 'E-Safe FRP Other Products', 'E-SAFE FRP SAFEHAND TOOL ANGLED J - HOOK WITH D HANDLE GRIP LENGTH 44 INCH'),
('MODEL: ES - TSTICK', 'E-SAFE MAGNETIC MATERIAL HANDLING TOOL', 'E-Safe FRP Other Products', 'E-SAFE MAGNETIC MATERIAL HANDLING TOOL Control Stik XL Extendable from 4 feet to 8 Feet'),
('E SAFE Cantilever Trolley ladder', 'ES-EMT', 'E-SAFE FIBRE GLASS LADDERS', 'E-Safe Fibre Glass Manually Extendable Cantilever Trolley ladder having side runners of FRP Box sect...'),
('E SAFE FRP Roof Ladder', 'ES-Roof ladder', 'E-Safe FRP Other Products', 'E Safe FRP Roof Ladder length with width 385mm step 150 mm wide with floor locking clips'),
('GRP Bird Cap', 'GRP Bird Cap', 'E-Safe FRP Other Products', 'E SAFE GRP Bird Cap of size 75mm x 75mm x 103mm H as per Drawing.'),
('E SAFE SAFE HAND SAFETY TOOLS', 'E SAFE SAFE HAND SAFETY TOOLS', 'E-Safe FRP Other Products', 'E SAFE SAFEHAND push/pull hand safety tool helps avoid hand injuries while working with suspended lo...'),
('ES-FRP Plain Sheet', 'FRP Plain Sheet', 'E-Safe FRP Other Products', 'E SAFE FRP Plain Sheet of Length, width and Thickness.'),
('ES-Accessories', 'Esafe Accessories for Discharge Rod', 'E-Safe FRP Other Products', 'Aluminium Discharge Head model ES-DR suitable for 40 mm Dia'),
('ES-Accessories Discharge Rod', 'E Safe Accessories', 'E-Safe FRP Other Products', 'Aluminum Earthing Clamp with T Handle for manual Tightening'),
('E SAFE FRP Grating', 'E SAFE FRP Grating', 'E-Safe FRP Other Products', 'FRP Grating of Size Length x Width x Thickness in 38mm x 38mm'),
('ES-PECST-H', 'ES-PECST-H', 'E-Safe FRP Other Products', 'E SAFE FRP Portable Electric Cable Support Towers'),
('E SAFE FRP A Type LADDERS Accessories', 'E SAFE FRP A Type LADDERS Accessories', 'E-Safe FRP Other Products', 'E SAFE FRP A Type LADDERS Accessories'),
('ES-EDS-650', 'ES-EDS-650', 'E-Safe FRP Other Products', 'E-SAFE Fibre Glass Discharge Device ULS 650 (Delivered Without Earthing Cable) Total Length 650mm is...'),
('ES-EDSW-650', 'ES-EDSW-650', 'E-Safe FRP Other Products', 'E-SAFE Fibre Glass Discharge Device ULS650-TPT8 (Delivered Without Earthing Cable) Total Length 650m...'),
('E SAFE FRP Step Stand Trolley Ladder', 'E SAFE FRP Step Stand Trolley Ladder', 'E-SAFE FIBRE GLASS LADDERS', 'E SAFE FRP Step Stand Trolley Ladder provided with Antiskid Working Platform at Height with antiskid...'),
('ES-TrenchCover', 'FRP Cable Trench Cover Slab', 'E-Safe FRP Other Products', 'FRP Cable Trench Cover Slab L X W X H'),
('VCB Clamp', 'VCB Clamp', 'E-Safe FRP Other Products', 'VCB Clamp Copper With 12mm Hole 165 + 39 x 45x6mm'),
('ACCESSORIES of FRP LADDERS', 'ACCESSORIES of FRP LADDERS', 'E-SAFE FIBRE GLASS LADDERS', 'ACCESSORIES of FRP LADDERS'),
('FRP Cable Trays 600mm Width', 'FRP Cable Trays 600mm Width', 'E-Safe FRP Other Products', 'E SAFE FRP Ladder type cable tray of size 600mmW x 100mmH Outside Height x 4mm Thickness with FRP Co...'),
('MAGNETIC DISC TOOLS', 'MAGNETIC DISC TOOLS', 'E-Safe FRP Other Products', 'MAGNETIC DISC TOOLS'),
('FRP SHEET', 'FRP SHEET', 'E-Safe FRP Other Products', 'FRP SHEET'),
('FRP Section', 'FRP Section', 'E-Safe FRP Other Products', 'E SAFE FRP Round Tube Inner Diameter');
