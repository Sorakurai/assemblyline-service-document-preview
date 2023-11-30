import os
import subprocess
import tempfile
from time import sleep, time

from assemblyline_v4_service.common.base import ServiceBase
from assemblyline_v4_service.common.request import ServiceRequest as Request
from assemblyline_v4_service.common.result import Heuristic, Result, ResultImageSection, ResultTextSection
from natsort import natsorted

from document_preview.helper.emlrender import processEml as eml2image


def pdfinfo_from_path(fp: str):
    pdfinfo = {}
    for info in subprocess.run(["pdfinfo", fp], capture_output=True).stdout.strip().decode().split("\n"):
        k, v = info.split(":", 1)
        # Clean up spacing
        v = v.lstrip()
        pdfinfo[k] = v
    pdfinfo


def convert_from_path(fp: str, output_directory: str, first_page=1, last_page=None):
    pdf_conv_command = ["pdftoppm", "-jpeg", "-f", str(first_page)]
    if last_page:
        pdf_conv_command += ["-l", str(last_page)]
    subprocess.run(pdf_conv_command + [fp, os.path.join(output_directory, "output")], capture_output=True)


class DocumentPreview(ServiceBase):
    def __init__(self, config=None):
        super(DocumentPreview, self).__init__(config)
        self.html_render_timeout = config.get("html_render_timeout", 30)

    def _start_unoserver_if_necessary(self):
        libre_pid_path = "/tmp/libre_pid"
        if not os.path.exists(libre_pid_path):
            self.log.info("PID FILE MISSING")
            # Start unoserver that is used for LibreOffice conversions to PDF
            subprocess.Popen(["unoserver", "-p", libre_pid_path])
            sleep(3)
            while not os.path.exists(libre_pid_path):
                # Continue sleeping until PID file is created
                self.log.info("SLEEPING")
                sleep(1)

    def start(self):
        self.log.debug("Document preview service started")
        self._start_unoserver_if_necessary()

    def stop(self):
        self.log.debug("Document preview service ended")

    def office_conversion(self, file, orientation="portrait", page_range_end=2):
        self._start_unoserver_if_necessary()
        proc = subprocess.run(
            [
                "unoconvert",
                "--convert-to",
                "pdf",
                "--filter-option",
                f"PageRange=1-{page_range_end}",
                "--filter-option",
                f"PaperOrientation={orientation}",
                "--filter-option",
                "PaperFormat=A3",
                "--dont-update-index",
                file,
                f"{self.working_directory}/converted.pdf",
            ],
            capture_output=True,
        )
        if "converted.pdf" in os.listdir(self.working_directory):
            return (True, "converted.pdf")
        else:
            raise Exception(proc.stderr)
            return (False, None)

    def pdf_to_images(self, file, max_pages=None):
        convert_from_path(file, self.working_directory, first_page=1, last_page=max_pages)

    def render_documents(self, request: Request, max_pages=1):
        # Word/Excel/Powerpoint/RTF
        if any(
            request.file_type == f"document/office/{ms_product}"
            for ms_product in ["word", "excel", "powerpoint", "rtf"]
        ):
            orientation = (
                "landscape" if any(request.file_type.endswith(type) for type in ["excel", "powerpoint"]) else "portrait"
            )
            converted = self.office_conversion(request.file_path, orientation, max_pages)
            if converted[0]:
                self.pdf_to_images(self.working_directory + "/" + converted[1])
        # PDF
        elif request.file_type == "document/pdf":
            self.pdf_to_images(request.file_path, max_pages)
        # EML/MSG
        elif request.file_type.endswith("email"):
            file_contents = request.file_contents
            # Convert MSG to EML where applicable
            if request.file_type == "document/office/email":
                with tempfile.NamedTemporaryFile() as tmp:
                    subprocess.run(
                        ["msgconvert", "-outfile", tmp.name, request.file_path],
                        capture_output=True,
                    )
                    tmp.seek(0)
                    file_contents = tmp.read()
            # Render EML as PNG
            # If we have internet access, we'll attempt to load external images
            eml2image(
                file_contents,
                self.working_directory,
                self.log,
                load_ext_images=False,
                load_images=request.get_param("load_email_images"),
            )
        # HTML
        elif request.file_type == "code/html":
            with tempfile.NamedTemporaryFile(suffix=".html") as tmp_html:
                tmp_html.write(request.file_contents)
                tmp_html.flush()
                with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp_pdf:
                    try:
                        subprocess.run(
                            [
                                "google-chrome",
                                "--headless",
                                "--no-sandbox",
                                "--hide-scrollbars",
                                f"--print-to-pdf={tmp_pdf.name}",
                                tmp_html.name,
                            ],
                            capture_output=True,
                            timeout=self.html_render_timeout,
                        )
                        self.pdf_to_images(tmp_pdf.name, max_pages)
                    except subprocess.TimeoutExpired:
                        # Unable to render HTML in given time
                        pass

    def execute(self, request):
        start = time()
        result = Result()

        # Attempt to render documents given and dump them to the working directory
        max_pages = int(request.get_param("max_pages_rendered"))
        save_ocr_output = request.get_param("save_ocr_output").lower()
        try:
            self.render_documents(request, max_pages)
        except Exception as e:
            # Unable to complete analysis after unexpected error, give up
            self.log.error(e)
            request.result = result
            return
        # Create an image gallery section to show the renderings
        if any("output" in s for s in os.listdir(self.working_directory)):
            previews = [s for s in os.listdir(self.working_directory) if "output" in s]
            image_section = ResultImageSection(request, "Preview Image(s)")
            run_ocr_on_first_n_pages = request.get_param("run_ocr_on_first_n_pages")
            for i, preview in enumerate(natsorted(previews)):
                # Trigger OCR on the first N pages as specified in the submission
                # Otherwise, just add the image without performing OCR analysis
                ocr_heur_id = 1 if request.deep_scan or (i < run_ocr_on_first_n_pages) else None
                ocr_io = tempfile.NamedTemporaryFile("w", delete=False)
                img_name = f"page_{str(i).zfill(3)}.jpeg"
                image_section.add_image(
                    f"{self.working_directory}/{preview}",
                    name=img_name,
                    description=f"Here's the preview for page {i}",
                    ocr_heuristic_id=ocr_heur_id,
                    ocr_io=ocr_io,
                )

                if request.file_type == "document/pdf":
                    with open(ocr_io.name, "r") as fp:
                        ocr_content = fp.read()
                    try:
                        if pdfinfo_from_path(request.file_path)["Pages"] == 1 and "click" in ocr_content.lower():
                            # Suspected document is part of a phishing campaign
                            ResultTextSection(
                                "Suspected Phishing",
                                body='Single-paged document containing the term "click"',
                                heuristic=Heuristic(2),
                                parent=result,
                            )
                    except Exception:
                        # There was a problem fetching the page count from the PDF, move on..
                        pass

                # Write OCR output as specified by submissions params
                if save_ocr_output == "no":
                    continue
                else:
                    # Write content to disk to be uploaded
                    if save_ocr_output == "as_extracted":
                        request.add_extracted(
                            ocr_io.name,
                            f"{img_name}_ocr_output",
                            description="OCR Output",
                        )
                    elif save_ocr_output == "as_supplementary":
                        request.add_supplementary(
                            ocr_io.name,
                            f"{img_name}_ocr_output",
                            description="OCR Output",
                        )
                    else:
                        self.log.warning(f"Unknown save method for OCR given: {save_ocr_output}")

            image_section.promote_as_screenshot()
            result.add_section(image_section)
        request.result = result
        self.log.debug(f"Runtime: {time() - start}s")
