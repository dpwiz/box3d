#!/usr/bin/env python3
"""Flatten Doxygen XML into a single Box3D public-API JSON file.

Reads the XML produced by Doxygen (configured in docs/CMakeLists.txt with
DOXYGEN_GENERATE_XML=YES) and writes a clean, stable schema to
api/box3d_api.json so binding generators in any language can consume it
without parsing C headers themselves.

Schema notes for binding generators:
- every item carries "group" (the Doxygen @defgroup id, "" if ungrouped) and
  "header"; the "groups" list gives each group's title, parent and prose
- "functions[].inline" is true for header-defined (static inline) helpers,
  which are not exported symbols and cannot be linked against
- descriptions are structured: "brief", "details" (prose paragraphs joined by
  blank lines), "params[].description", "returns", "notes", "warnings", "see",
  "code" (verbatim @code blocks)
- "structs[].kind" is "struct" or "union"; anonymous nested aggregates appear
  as their own entries named "<Parent>.<field>" or "<Parent>.__unnamedN__",
  and the parent field's "type" names that entry so the layout can be rebuilt
- "line" is the declaration's line in "header", to recover source order
- struct fields carry "args" (array extent such as "[8]", or the parameter
  list of a function-pointer field) and "bitfield" (width, or "")
"""
import argparse
import copy
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def text_of(el):
    """Flatten an element to one line of text, keeping inline code in backticks."""
    if el is None:
        return ""
    parts = []

    def walk(node, root):
        if node.tag == "computeroutput":
            parts.append("`")
        parts.append(node.text or "")
        for child in node:
            walk(child, False)
        if node.tag == "computeroutput":
            parts.append("`")
        if not root:
            parts.append(node.tail or "")

    walk(el, True)
    return " ".join("".join(parts).split())


_TYPE_NOISE = re.compile(r"^B\d_(API|INLINE)$")


def clean_type(s):
    return " ".join(t for t in s.split() if not _TYPE_NOISE.match(t))


def clean_initializer(s):
    s = s.strip()
    return s[1:].strip() if s.startswith("=") else s


def header_of(el):
    loc = el.find("location")
    if loc is None:
        return ""
    return os.path.basename(loc.get("file", ""))


def line_of(el):
    loc = el.find("location")
    if loc is None:
        return 0
    return int(loc.get("line", "0"))


def has_body(el):
    loc = el.find("location")
    return loc is not None and loc.get("bodystart") is not None


def code_lines(listing):
    for sp in listing.iter("sp"):
        sp.text = " "
    return [
        "".join(cl.itertext()).rstrip() for cl in listing.findall("codeline")
    ]


def parse_description(desc):
    """Split a Doxygen description into prose and its structured parts."""
    out = {
        "details": "",
        "params": {},
        "returns": "",
        "notes": [],
        "warnings": [],
        "see": [],
        "code": [],
    }
    if desc is None:
        return out
    desc = copy.deepcopy(desc)
    parents = {child: parent for parent in desc.iter() for child in parent}
    lifted = []
    for el in desc.iter():
        if el.tag in ("parameterlist", "simplesect", "programlisting"):
            lifted.append(el)
    for el in lifted:
        if el.tag == "parameterlist":
            if el.get("kind") == "param":
                for item in el.findall("parameteritem"):
                    for pn in item.findall("parameternamelist/parametername"):
                        out["params"][text_of(pn)] = text_of(
                            item.find("parameterdescription")
                        )
        elif el.tag == "simplesect":
            kind = el.get("kind")
            body = text_of(el)
            if kind == "return":
                out["returns"] = (out["returns"] + " " + body).strip()
            elif kind == "note":
                out["notes"].append(body)
            elif kind == "warning":
                out["warnings"].append(body)
            elif kind == "see":
                out["see"].append(body)
        else:
            out["code"].append("\n".join(code_lines(el)))
        parents[el].remove(el)
    paras = [text_of(p) for p in desc.findall("para")]
    out["details"] = "\n\n".join(p for p in paras if p)
    return out


def parse_function(md, group):
    d = parse_description(md.find("detaileddescription"))
    return {
        "name": md.findtext("name", "").strip(),
        "return_type": clean_type(text_of(md.find("type"))),
        "params": [
            {
                "name": (p.findtext("declname") or "").strip(),
                "type": clean_type(text_of(p.find("type"))),
                "description": d["params"].get(
                    (p.findtext("declname") or "").strip(), ""
                ),
            }
            for p in md.findall("param")
        ],
        "header": header_of(md),
        "line": line_of(md),
        "group": group,
        "inline": has_body(md),
        "brief": text_of(md.find("briefdescription")),
        "details": d["details"],
        "returns": d["returns"],
        "notes": d["notes"],
        "warnings": d["warnings"],
        "see": d["see"],
        "code": d["code"],
    }


def parse_typedef(md, group):
    return {
        "name": md.findtext("name", "").strip(),
        "type": clean_type(text_of(md.find("type"))),
        "args": text_of(md.find("argsstring")),
        "header": header_of(md),
        "line": line_of(md),
        "group": group,
        "brief": text_of(md.find("briefdescription")),
        "details": parse_description(md.find("detaileddescription"))["details"],
    }


def parse_enum(md, group):
    return {
        "name": md.findtext("name", "").strip(),
        "values": [
            {
                "name": ev.findtext("name", "").strip(),
                "value": clean_initializer(text_of(ev.find("initializer"))),
                "brief": text_of(ev.find("briefdescription")),
            }
            for ev in md.findall("enumvalue")
        ],
        "header": header_of(md),
        "line": line_of(md),
        "group": group,
        "brief": text_of(md.find("briefdescription")),
        "details": parse_description(md.find("detaileddescription"))["details"],
    }


def parse_macro(md, group):
    return {
        "name": md.findtext("name", "").strip(),
        "params": [(p.findtext("defname") or "").strip() for p in md.findall("param")],
        "value": text_of(md.find("initializer")),
        "header": header_of(md),
        "line": line_of(md),
        "group": group,
        "brief": text_of(md.find("briefdescription")),
    }


_NESTED = re.compile(r"^(struct|union) (\S+)$")


def parse_struct(cd, group):
    name = cd.findtext("compoundname", "").strip()
    root = name.split(".")[0]
    fields = []
    unnamed = 0
    for sd in cd.findall("sectiondef"):
        for md in sd.findall("memberdef"):
            if md.get("kind") != "variable":
                continue
            field_name = md.findtext("name", "").strip()
            field_type = clean_type(text_of(md.find("type")))
            nested = _NESTED.match(field_type)
            if nested and nested.group(2) == root:
                if field_name:
                    field_type = name + "." + field_name
                else:
                    field_type = name + ".__unnamed%d__" % unnamed
                    unnamed += 1
            fields.append(
                {
                    "name": field_name,
                    "type": field_type,
                    "args": text_of(md.find("argsstring")),
                    "bitfield": (md.findtext("bitfield") or "").strip(),
                    "brief": text_of(md.find("briefdescription")),
                    "details": parse_description(md.find("detaileddescription"))[
                        "details"
                    ],
                }
            )
    return {
        "name": name,
        "kind": cd.get("kind"),
        "fields": fields,
        "header": header_of(cd),
        "line": line_of(cd),
        "group": group,
        "brief": text_of(cd.find("briefdescription")),
        "details": parse_description(cd.find("detaileddescription"))["details"],
    }


def parse_group(cd):
    return {
        "name": cd.findtext("compoundname", "").strip(),
        "title": text_of(cd.find("title")),
        "parent": "",
        "brief": text_of(cd.find("briefdescription")),
        "details": parse_description(cd.find("detaileddescription"))["details"],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--xml", default="build/docs/xml", help="Doxygen XML output dir")
    ap.add_argument("--out", default="api/box3d_api.json", help="output JSON path")
    ap.add_argument("--version", default="", help="Box3D version string")
    args = ap.parse_args()

    xml_dir = Path(args.xml)
    if not xml_dir.is_dir():
        sys.exit(f"error: xml dir not found: {xml_dir}")

    index = ET.parse(xml_dir / "index.xml").getroot()

    api = {
        "version": args.version,
        "files": [],
        "groups": [],
        "macros": [],
        "typedefs": [],
        "enums": [],
        "structs": [],
        "functions": [],
    }

    compounds = []
    for compound in index.findall("compound"):
        refid = compound.get("refid")
        cd = ET.parse(xml_dir / f"{refid}.xml").getroot().find("compounddef")
        if cd is not None:
            compounds.append((compound.get("kind"), refid, cd))

    # Groups first: their members carry the group id, and dedup keeps the
    # first occurrence, so an ungrouped duplicate from a file never wins.
    compounds.sort(key=lambda c: c[0] != "group")

    struct_group = {}
    group_parent = {}
    for kind, refid, cd in compounds:
        if kind != "group":
            continue
        name = cd.findtext("compoundname", "").strip()
        for ic in cd.findall("innerclass"):
            struct_group[ic.get("refid")] = name
        for ig in cd.findall("innergroup"):
            group_parent[ig.get("refid")] = name

    for kind, refid, cd in compounds:
        if kind == "group":
            g = parse_group(cd)
            g["parent"] = group_parent.get(refid, "")
            api["groups"].append(g)
            group = g["name"]
        else:
            group = ""

        if kind == "file":
            name = cd.findtext("compoundname", "").strip()
            if name.endswith(".h"):
                api["files"].append(name)
        if kind in ("file", "group"):
            for sd in cd.findall("sectiondef"):
                for md in sd.findall("memberdef"):
                    mkind = md.get("kind")
                    if mkind == "function":
                        api["functions"].append(parse_function(md, group))
                    elif mkind == "typedef":
                        api["typedefs"].append(parse_typedef(md, group))
                    elif mkind == "enum":
                        api["enums"].append(parse_enum(md, group))
                    elif mkind == "define":
                        api["macros"].append(parse_macro(md, group))
        elif kind in ("struct", "union"):
            api["structs"].append(parse_struct(cd, struct_group.get(refid, "")))

    def dedup(items):
        seen = set()
        out = []
        for it in items:
            key = (it.get("name", ""), it.get("header", ""))
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
        return out

    api["files"] = sorted(set(api["files"]))
    for key in ("groups", "macros", "typedefs", "enums", "structs", "functions"):
        api[key] = sorted(dedup(api[key]), key=lambda x: x.get("name", ""))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(api, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(
        f"wrote {out} "
        f"({len(api['functions'])} functions, "
        f"{len(api['structs'])} structs, "
        f"{len(api['enums'])} enums, "
        f"{len(api['typedefs'])} typedefs, "
        f"{len(api['macros'])} macros, "
        f"{len(api['groups'])} groups)"
    )


if __name__ == "__main__":
    main()
