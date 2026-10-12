# ─── Obsidian/Notion Export ───

def export_to_obsidian(
    content_items: List[Dict[str, Any]],
    output_dir: Path,
    include_assets: bool = True,
    bidir_links: bool = True,
) -> Dict[str, Any]:
    """Export canonical content items to Obsidian-compatible markdown with frontmatter.
    
    Creates bidirectional links between related notes using [[wiki-link]] syntax.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    if include_assets:
        assets_dir = output_dir / "assets"
        assets_dir.mkdir(exist_ok=True)
    
    created_files = []
    note_map = {}  # topic -> filename mapping for wikilinks
    
    # First pass: collect all topics for wikilink resolution
    for item in content_items:
        topic = item.get("topic", "Untitled")
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', topic)
        note_map[topic] = safe_name
    
    # Second pass: generate markdown files
    for item in content_items:
        topic = item.get("topic", "Untitled")
        safe_name = note_map[topic]
        content = item.get("content", {})
        
        # Build frontmatter
        frontmatter = {
            "title": topic,
            "tags": ["medforge", "medical", "study"],
            "created": item.get("created_at", ""),
            "source": "medforge",
            "content_id": item.get("content_id", ""),
            "evidence_refs": [ref.get("label", "") for ref in content.get("evidence_refs", []) if ref.get("label")],
        }
        
        # Build markdown body
        lines = []
        
        # Learning objectives
        objectives = content.get("learning_objectives", [])
        if objectives:
            lines.append("## Learning Objectives")
            for obj in objectives:
                lines.append(f"- {obj}")
            lines.append("")
        
        # Key facts with evidence links
        key_facts = content.get("key_facts", [])
        if key_facts:
            lines.append("## Key Facts")
            for fact in key_facts:
                fact_text = fact.get("fact", "")
                refs = fact.get("refs", [])
                if refs:
                    ref_links = " ".join([f"[[{r}]]" for r in refs])
                    fact_text = f"{fact_text} {ref_links}"
                lines.append(f"- {fact_text}")
            lines.append("")
        
        # Mechanisms
        mechanisms = content.get("mechanisms", [])
        if mechanisms:
            lines.append("## Mechanisms")
            for mech in mechanisms:
                name = mech.get("name", "")
                steps = mech.get("steps", [])
                lines.append(f"### {name}")
                for i, step in enumerate(steps, 1):
                    lines.append(f"{i}. {step}")
                lines.append("")
        
        # Definitions
        definitions = content.get("definitions", [])
        if definitions:
            lines.append("## Definitions")
            for defn in definitions:
                term = defn.get("term", "")
                meaning = defn.get("meaning", "")
                refs = defn.get("refs", [])
                if refs:
                    ref_links = " ".join([f"[[{r}]]" for r in refs])
                    meaning = f"{meaning} {ref_links}"
                lines.append(f"- **{term}**: {meaning}")
            lines.append("")
        
        # Relationships
        relationships = content.get("relationships", [])
        if relationships:
            lines.append("## Relationships")
            for rel in relationships:
                a = rel.get("a", "")
                b = rel.get("b", "")
                relation = rel.get("relation", "")
                refs = rel.get("refs", [])
                ref_links = " ".join([f"[[{r}]]" for r in refs])
                lines.append(f"- {a} → {b} ({relation}) {ref_links}")
            lines.append("")
        
        # Clinical correlations
        clinical = content.get("clinical_correlations", [])
        if clinical:
            lines.append("## Clinical Correlations")
            for corr in clinical:
                point = corr.get("point", "")
                refs = corr.get("refs", [])
                ref_links = " ".join([f"[[{r}]]" for r in refs])
                lines.append(f"- {point} {ref_links}")
            lines.append("")
        
        # Misconceptions
        misconceptions = content.get("misconceptions", [])
        if misconceptions:
            lines.append("## Common Misconceptions")
            for misc in misconceptions:
                wrong = misc.get("wrong", "")
                correction = misc.get("correction", "")
                refs = misc.get("refs", [])
                ref_links = " ".join([f"[[{r}]]" for r in refs])
                lines.append(f"- ❌ **{wrong}** → ✅ {correction} {ref_links}")
            lines.append("")
        
        # High yield
        high_yield = content.get("high_yield", [])
        if high_yield:
            lines.append("## High Yield")
            for item_text in high_yield:
                lines.append(f"- {item_text}")
            lines.append("")
        
        # Add wikilinks to related topics
        if bidir_links:
            # Find related topics from evidence_refs
            related_topics = set()
            for ref in content.get("evidence_refs", []):
                source = ref.get("source", "")
                if source and source != topic:
                    related_topics.add(source)
            if related_topics:
                lines.append("## Related Topics")
                for rt in sorted(related_topics):
                    if rt in note_map:
                        lines.append(f"- [[{note_map[rt]}]]")
                    else:
                        lines.append(f"- {rt}")
                lines.append("")
        
        # Write file
        filename = f"{safe_name}.md"
        filepath = Path(output_dir) / filename
        
        # YAML frontmatter
        fm_lines = ["---"]
        for key, value in frontmatter.items():
            if isinstance(value, list):
                fm_lines.append(f"{key}: {json.dumps(value)}")
            else:
                fm_lines.append(f"{key}: \"{value}\"")
        fm_lines.append("---")
        fm_lines.append("")
        
        markdown_content = "\n".join(fm_lines) + "\n".join(lines)
        filepath.write_text(markdown_content, encoding="utf-8")
        created_files.append(str(filepath))
    
    return {
        "exported": len(created_files),
        "files": created_files,
        "output_dir": str(output_dir),
    }


def export_to_notion(
    content_items: List[Dict[str, Any]],
    output_dir: Path,
) -> Dict[str, Any]:
    """Export canonical content items to Notion-compatible CSV/JSON format.
    
    Notion supports CSV import with specific column mapping.
    """
    import csv
    from pathlib import Path
    
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    csv_path = output_dir / "notion_import.csv"
    json_path = output_dir / "notion_import.json"
    
    # Notion CSV columns
    fieldnames = [
        "Name", "Type", "Status", "Topic", "Tags", "Created", 
        "Content", "Evidence Refs", "Content ID"
    ]
    
    created_files = []
    
    # Write CSV
    with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        
        for item in content_items:
            topic = item.get("topic", "Untitled")
            content = item.get("content", {})
            
            # Build content summary
            content_parts = []
            for key_fact in content.get("key_facts", [])[:5]:
                content_parts.append(f"• {key_fact.get('fact', '')}")
            
            row = {
                "Name": item.get("topic", "Untitled"),
                "Type": "Study Material",
                "Status": "Draft",
                "Topic": item.get("topic", ""),
                "Tags": "medforge,medical,study",
                "Created": item.get("created_at", ""),
                "Content": "\n".join(content_parts),
                "Evidence Refs": ", ".join([ref.get("label", "") for ref in content.get("evidence_refs", []) if ref.get("label")]),
                "Content ID": item.get("content_id", ""),
            }
            writer.writerow(row)
    
    # Write JSON (more complete data for Notion API)
    notion_json = []
    for item in content_items:
        content = item.get("content", {})
        notion_json.append({
            "properties": {
                "Name": {"title": [{"text": {"content": item.get("topic", "Untitled")}}]},
                "Type": {"select": {"name": "Study Material"}},
                "Status": {"select": {"name": "Draft"}},
                "Topic": {"rich_text": [{"text": {"content": item.get("topic", "")}}]},
                "Tags": {"multi_select": [{"name": "medforge"}, {"name": "medical"}, {"name": "study"}]},
                "Created": {"date": {"start": item.get("created_at", "")}},
                "Content": {"rich_text": [{"text": {"content": str(content.get("key_facts", []))[:2000]}}]},
                "Evidence Refs": {"rich_text": [{"text": {"content": ", ".join([ref.get("label", "") for ref in content.get("evidence_refs", []) if ref.get("label")])}}]},
                "Content ID": {"rich_text": [{"text": {"content": item.get("content_id", "")}}]},
            }
        }
    
    json_path.write_text(json.dumps(notion_json, indent=2), encoding="utf-8")
    created_files.append(str(csv_path))
    created_files.append(str(json_path))
    
    return {
        "exported": len(created_files) - 1,  # CSV count
        "files": created_files,
        "output_dir": str(output_dir),
    }