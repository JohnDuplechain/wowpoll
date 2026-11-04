"""Build WowPoll's tier-list icon library from the forever-icons black-frame pack.

Icons are Blizzard Entertainment's artwork (fan use; see README). The pack is a
zip of BLP game textures: https://github.com/hastecraft/forever-icons/releases

Run:  python tools/build_wow_icons.py path/to/forever-icons-black-frame-*.zip
Writes static/wow-icons/<slug>.webp, index.json (searchable slugs) and sets.json.
Icons are read from the zip in memory; nothing from the zip is written to disk.
Needs pillow (dev only).
"""
import io
import json
import re
import sys
import zipfile
from pathlib import Path

from PIL import Image

SIZE = 96          # px; tiles show at 56px, so this stays sharp on hi-dpi screens
QUALITY = 80
OUT = Path(__file__).resolve().parent.parent / "static" / "wow-icons"

# Item icons (inv_*) are 25k+, so only keep the kinds people put in tier lists.
INV_KEEP = ("mount", "pet", "misc_head", "tabard", "trinket", "relics", "misc_book", "elemental",
            "_sword_", "_axe_", "_mace_", "_staff_", "_bow_", "_shield_", "tournaments_symbol",
            "firstaid", "inscription_tradeskill", "misc_pelt_wolf_01", "misc_food_15", "misc_gem_01")

SETS = {
    "Classic classes": [
        ("Warrior", "classicon_warrior"), ("Paladin", "classicon_paladin"), ("Hunter", "classicon_hunter"),
        ("Rogue", "classicon_rogue"), ("Priest", "classicon_priest"), ("Shaman", "classicon_shaman"),
        ("Mage", "classicon_mage"), ("Warlock", "classicon_warlock"), ("Druid", "classicon_druid"),
    ],
    "All classes": [
        ("Warrior", "classicon_warrior"), ("Paladin", "classicon_paladin"), ("Hunter", "classicon_hunter"),
        ("Rogue", "classicon_rogue"), ("Priest", "classicon_priest"), ("Death Knight", "classicon_deathknight"),
        ("Shaman", "classicon_shaman"), ("Mage", "classicon_mage"), ("Warlock", "classicon_warlock"),
        ("Monk", "classicon_monk"), ("Druid", "classicon_druid"), ("Demon Hunter", "classicon_demonhunter"),
        ("Evoker", "classicon_evoker"),
    ],
    "Races": [
        ("Human", "achievement_character_human_male"), ("Dwarf", "achievement_character_dwarf_male"),
        ("Night Elf", "achievement_character_nightelf_female"), ("Gnome", "achievement_character_gnome_male"),
        ("Draenei", "achievement_character_draenei_female"), ("Orc", "achievement_character_orc_male"),
        ("Undead", "achievement_character_undead_male"), ("Tauren", "achievement_character_tauren_male"),
        ("Troll", "achievement_character_troll_male"), ("Blood Elf", "achievement_character_bloodelf_female"),
    ],
    "Professions": [
        ("Alchemy", "trade_alchemy"), ("Blacksmithing", "trade_blacksmithing"),
        ("Enchanting", "trade_engraving"), ("Engineering", "trade_engineering"),
        ("Herbalism", "trade_herbalism"), ("Leatherworking", "trade_leatherworking"),
        ("Mining", "trade_mining"), ("Skinning", "inv_misc_pelt_wolf_01"), ("Tailoring", "trade_tailoring"),
        ("Jewelcrafting", "inv_misc_gem_01"), ("Inscription", "inv_inscription_tradeskill01"),
        ("Cooking", "inv_misc_food_15"), ("Fishing", "trade_fishing"), ("First Aid", "spell_holy_sealofsacrifice"),
    ],
    "Classic raid bosses": [
        ("Ragnaros", "achievement_boss_ragnaros"), ("Onyxia", "achievement_boss_onyxia"),
        ("Nefarian", "achievement_dungeon_blackwingdescent_raid_nefarian"), ("Hakkar", "achievement_boss_hakkar"),
        ("Ossirian", "achievement_boss_ossiriantheunscarred"), ("C'Thun", "achievement_boss_cthun"),
        ("Kel'Thuzad", "achievement_boss_kelthuzad_01"),
    ],
    "Factions": [("Alliance", "ui_allianceicon"), ("Horde", "ui_hordeicon")],
}


def slug(name: str) -> str:
    name = name.lower().removesuffix(".tga")
    return re.sub(r"[^a-z0-9_]+", "-", name).strip("-_")


def wanted(name: str) -> bool:
    return not name.startswith("inv") or any(k in name for k in INV_KEEP)


def main(zip_path: str) -> None:
    z = zipfile.ZipFile(zip_path)
    entries = {}
    for info in z.infolist():
        path = info.filename.replace(chr(92), "/")
        if not path.lower().endswith(".blp") or "/" not in path:
            continue
        raw = path.rsplit("/", 1)[-1][:-4].lower()
        s = slug(raw)
        if s and s not in entries:
            entries[s] = (raw, info)

    set_slugs = {icon for items in SETS.values() for _, icon in items}
    keep = sorted(s for s, (raw, _) in entries.items() if wanted(raw) or s in set_slugs)

    OUT.mkdir(parents=True, exist_ok=True)
    written, failed, total_bytes = [], [], 0
    for i, s in enumerate(keep, 1):
        raw, info = entries[s]
        try:
            with z.open(info) as f:
                im = Image.open(io.BytesIO(f.read()))
                im.load()
            im = im.convert("RGBA").resize((SIZE, SIZE), Image.Resampling.LANCZOS)
            dest = OUT / f"{s}.webp"
            im.save(dest, "WEBP", quality=QUALITY, method=4)
            total_bytes += dest.stat().st_size
            written.append(s)
        except Exception as e:  # a few textures may be unreadable; skip them
            failed.append((s, str(e)[:80]))
        if i % 1000 == 0:
            print(f"  {i}/{len(keep)}")

    ok = set(written)
    sets = {
        name: [{"name": label, "icon": icon} for label, icon in items if icon in ok]
        for name, items in SETS.items()
    }
    (OUT / "index.json").write_text(json.dumps(written, separators=(",", ":")))
    (OUT / "sets.json").write_text(json.dumps(sets, indent=1))
    print(f"wrote {len(written)} icons ({total_bytes / 1e6:.1f} MB) to {OUT}")
    print(f"skipped {len(failed)} unreadable: {failed[:5]}")
    missing = [icon for items in SETS.values() for _, icon in items if icon not in ok]
    print("set icons missing:", missing)


if __name__ == "__main__":
    main(sys.argv[1])
