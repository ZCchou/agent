# Document task examples

## Extraction and comparison

For a request to extract dates and requirements from two attachments, read the relevant ranges of both documents with document.read_attachment. Keep each value associated with its attachment and returned page, section, or paragraph locator. Continue pagination until the requested coverage is complete. Mark absent values as missing and report conflicting statements with both sources.

## Revising an existing DOCX

For a request to update a report using a second attachment, read the supporting attachment and inspect the report with document.inspect_template. Select the editable target IDs that correspond to the requested changes. Fill those targets, preserve useful existing structure, and export the returned artifact when the user asked for a saved document. A report named source.docx may be the editable document, and a file named template.docx may provide source facts. Resolve the task through the request and contents.

## Evidence table example

For a professional compliance review, read the submitted document and retrieve the applicable project evidence. Link each reviewed claim to its document locator, the applicable evidence ID and quote ID, the relevant applicability conditions, and any unresolved gap. A useful output table contains the reviewed statement, source location, evidence, assessment, and needed correction. Keep unsupported conclusions pending. Use document.review_targets when the current tool capability provides the managed review workflow, then perform any requested revision and export.

## Plain document summary

For a summary of an attached meeting record, read the record and summarize the requested decisions and actions with source locators. This task can be completed from the attachment itself. Additional retrieval is appropriate when the user asks to verify the statements against project evidence.
