"""Transform OpenSanctions CSVs into our watchlist_records schema."""
import csv
import os
import sys
from datetime import datetime

csv.field_size_limit(sys.maxsize)

FIELDS = ['source_agency', 'source_list', 'case_unit', 'name', 'father_name',
          'date_of_birth', 'gender', 'address', 'reward_amount', 'details',
          'has_document', 'document_url', 'detail_page_url', 'interpol_notice_id',
          'link_kind', 'scraped_at', 'enrichment_status']


def transform_opensanctions(input_path, agency, list_name, output_path, link_kind_default):
    now = datetime.utcnow().isoformat()[:19]
    count = 0
    with open(input_path, encoding='utf-8') as fin, \
         open(output_path, 'w', newline='', encoding='utf-8') as fout:
        reader = csv.DictReader(fin)
        writer = csv.DictWriter(fout, fieldnames=FIELDS)
        writer.writeheader()
        for r in reader:
            name = (r.get('name') or '').strip()
            if not name or len(name) < 2:
                continue
            parts = []
            if r.get('aliases'):
                parts.append(f"Aliases: {r['aliases'][:200]}")
            if r.get('countries'):
                parts.append(f"Countries: {r['countries']}")
            if r.get('identifiers'):
                parts.append(f"IDs: {r['identifiers'][:200]}")
            if r.get('sanctions'):
                parts.append(f"Programs: {r['sanctions'][:200]}")
            if r.get('program_ids'):
                parts.append(f"ProgramIDs: {r['program_ids'][:200]}")
            if r.get('dataset'):
                parts.append(f"Sources: {r['dataset'][:200]}")
            writer.writerow({
                'source_agency': agency,
                'source_list': list_name,
                'case_unit': r.get('schema') or '',
                'name': name[:500],
                'father_name': '',
                'date_of_birth': r.get('birth_date') or '',
                'gender': '',
                'address': (r.get('addresses') or '')[:500],
                'reward_amount': '',
                'details': ' | '.join(parts)[:1000],
                'has_document': '',
                'document_url': '',
                'detail_page_url': f"https://www.opensanctions.org/entities/{r.get('id', '')}/",
                'interpol_notice_id': '',
                'link_kind': link_kind_default,
                'scraped_at': now,
                'enrichment_status': '',
            })
            count += 1
    print(f"  {os.path.basename(input_path)}: {count:,} rows -> {output_path}")
    return count


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    total = 0
    for inp, ag, ln, out, lk in [
        ("data/opensanctions/debarment.csv", "OpenSanctions",
         "Debarment (Development Banks)", "data/opensanctions_debarment.csv", "debarment"),
        ("data/opensanctions/crime.csv", "OpenSanctions",
         "Crime (Criminal Interest)", "data/opensanctions_crime.csv", "criminal"),
        ("data/opensanctions/peps.csv", "OpenSanctions",
         "Politically Exposed Persons (PEPs)", "data/opensanctions_peps.csv", "pep"),
    ]:
        if os.path.exists(inp):
            total += transform_opensanctions(inp, ag, ln, out, lk)
    print(f"Total OpenSanctions rows: {total:,}")
