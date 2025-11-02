# test_extractor.py

from extractor import extract_data_from_pdf

# Replace this with your actual sample PDF file
pdf_path = "sample.pdf"

# Run the extractor
df = extract_data_from_pdf(pdf_path)

# Show first few rows
print(df.head())

# Save to CSV
df.to_csv("extracted_output.csv", index=False)
print("✅ CSV saved as extracted_output.csv")
