"""Bank Statement -> CSV Converter

Drag in (or open) PDF bank statements, review/fix the parsed transactions in
an editable table, then export a clean Date / Description / Amount CSV ready
for import into budgeting or accounting software.
"""
from __future__ import annotations

import csv
import os
import tempfile
import threading
import tkinter as tk
import webbrowser
from tkinter import ttk, filedialog, messagebox, simpledialog

from tkinterdnd2 import DND_FILES, TkinterDnD

import license_client
import updater
from parser import PdfPasswordRequired, extract_rows, parse_date, reconcile

DATE_FORMATS = {
    "YYYY-MM-DD": "%Y-%m-%d",
    "MM/DD/YYYY": "%m/%d/%Y",
    "DD/MM/YYYY": "%d/%m/%Y",
}


class EditableTreeview(ttk.Treeview):
    """A Treeview where double-clicking a cell lets you edit its text in place."""

    def __init__(self, master, on_change, **kwargs):
        super().__init__(master, **kwargs)
        self._on_change = on_change
        self._editor: tk.Entry | None = None
        self.bind("<Double-1>", self._begin_edit)

    def _begin_edit(self, event):
        region = self.identify("region", event.x, event.y)
        if region != "cell":
            return
        row_id = self.identify_row(event.y)
        col_id = self.identify_column(event.x)
        if not row_id or not col_id:
            return
        col_index = int(col_id.replace("#", "")) - 1
        if col_index < 0:
            return

        x, y, width, height = self.bbox(row_id, col_id)
        value = self.set(row_id, self["columns"][col_index])

        self._editor = tk.Entry(self)
        self._editor.insert(0, value)
        self._editor.select_range(0, tk.END)
        self._editor.focus()
        self._editor.place(x=x, y=y, width=width, height=height)

        def commit(_event=None):
            if self._editor is None:
                return
            new_value = self._editor.get()
            self.set(row_id, self["columns"][col_index], new_value)
            self._editor.destroy()
            self._editor = None
            self._on_change(row_id, self["columns"][col_index], new_value)

        def cancel(_event=None):
            if self._editor is not None:
                self._editor.destroy()
                self._editor = None

        self._editor.bind("<Return>", commit)
        self._editor.bind("<KP_Enter>", commit)
        self._editor.bind("<Escape>", cancel)
        self._editor.bind("<FocusOut>", commit)


class AccountDialog(tk.Toplevel):
    """Sign-in/sign-up form, or an account summary with a sign-out button."""

    def __init__(self, master, on_change):
        super().__init__(master)
        self.title("Account")
        self.resizable(False, False)
        self.transient(master)
        self._on_change = on_change
        self._build()
        self.grab_set()

    def _build(self):
        for w in self.winfo_children():
            w.destroy()
        if license_client.is_signed_in():
            self._build_signed_in()
        else:
            self._build_sign_in_form()

    def _build_signed_in(self):
        frame = ttk.Frame(self, padding=16)
        frame.pack()
        email = license_client.current_email() or "your account"
        ttk.Label(frame, text=f"Signed in as {email}.").pack(pady=(0, 12))
        ttk.Button(frame, text="Upgrade to Pro...", command=self._upgrade).pack(pady=(0, 6))
        ttk.Button(frame, text="Sign Out", command=self._sign_out).pack()

    def _upgrade(self):
        self.master.open_upgrade_checkout()

    def _sign_out(self):
        license_client.sign_out()
        self._on_change()
        self.destroy()

    def _build_sign_in_form(self):
        frame = ttk.Frame(self, padding=16)
        frame.pack()

        ttk.Label(frame, text="Email:").grid(row=0, column=0, sticky=tk.W, pady=4)
        email_var = tk.StringVar()
        email_entry = ttk.Entry(frame, textvariable=email_var, width=32)
        email_entry.grid(row=0, column=1, pady=4)
        email_entry.focus()

        ttk.Label(frame, text="Password:").grid(row=1, column=0, sticky=tk.W, pady=4)
        password_var = tk.StringVar()
        ttk.Entry(frame, textvariable=password_var, show="*", width=32).grid(row=1, column=1, pady=4)

        status_var = tk.StringVar()
        ttk.Label(frame, textvariable=status_var, foreground="red", wraplength=320).grid(
            row=2, column=0, columnspan=2, pady=(4, 0)
        )

        def sign_in():
            try:
                license_client.sign_in(email_var.get().strip(), password_var.get())
            except license_client.LicenseError as exc:
                status_var.set(str(exc))
                return
            self._on_change()
            self.destroy()

        def sign_up():
            try:
                license_client.sign_up(email_var.get().strip(), password_var.get())
            except license_client.LicenseError as exc:
                status_var.set(str(exc))
                return
            if license_client.is_signed_in():
                self._on_change()
                self.destroy()
            else:
                messagebox.showinfo("Check your email", "Confirm your email, then sign in.", parent=self)
                self._build()

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(buttons, text="Sign In", command=sign_in).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="Sign Up", command=sign_up).pack(side=tk.LEFT, padx=4)


class ReportIssueDialog(tk.Toplevel):
    """Opt-in "report a bad conversion" form. Nothing is sent until the user
    fills this in and clicks Send -- see license_client.submit_bug_report()
    and backend/supabase/migrations/0002_bug_reports.sql."""

    def __init__(self, master: "App"):
        super().__init__(master)
        self.title("Report an Issue")
        self.resizable(False, False)
        self.transient(master)
        self._master_app = master
        self._build()
        self.grab_set()

    def _build(self):
        frame = ttk.Frame(self, padding=16)
        frame.pack()

        ttk.Label(frame, text="Bank / statement type:").grid(row=0, column=0, sticky=tk.W, pady=4)
        bank_var = tk.StringVar(value=self._master_app._last_bank_name)
        ttk.Entry(frame, textvariable=bank_var, width=48).grid(row=0, column=1, pady=4, sticky=tk.W)

        ttk.Label(frame, text="What went wrong:").grid(row=1, column=0, sticky=tk.NW, pady=4)
        details_text = tk.Text(frame, width=48, height=6, wrap=tk.WORD)
        details_text.grid(row=1, column=1, pady=4, sticky=tk.W)

        include_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame,
            text="Include the parsed table shown below (dates, amounts, descriptions)",
            variable=include_var,
        ).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(10, 0))
        ttk.Label(
            frame,
            text="Off by default. Descriptions can contain merchant/payee names. The original\n"
            "PDF is never sent either way -- only what you explicitly check here.",
            foreground="gray30",
            justify=tk.LEFT,
        ).grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(0, 10))

        status_var = tk.StringVar()
        status_label = ttk.Label(frame, textvariable=status_var, wraplength=380)
        status_label.grid(row=4, column=0, columnspan=2, sticky=tk.W)

        def send():
            details = details_text.get("1.0", tk.END).strip()
            if not details:
                status_var.set("Please describe what went wrong.")
                return
            sample_rows = list(self._master_app.rows.values()) if include_var.get() else None
            send_button.state(["disabled"])
            status_var.set("Sending...")

            def worker():
                try:
                    license_client.submit_bug_report(
                        bank_name=bank_var.get().strip(),
                        details=details,
                        app_version=updater.APP_VERSION,
                        used_ocr=self._master_app._any_used_ocr,
                        sample_rows=sample_rows,
                    )
                except license_client.LicenseError as exc:
                    self.after(0, lambda: self._finish(status_var, send_button, f"Couldn't send: {exc}"))
                    return
                self.after(0, lambda: self._finish(status_var, send_button, None))

            threading.Thread(target=worker, daemon=True).start()

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, pady=(10, 0))
        send_button = ttk.Button(buttons, text="Send", command=send)
        send_button.pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.LEFT, padx=4)

    def _finish(self, status_var: tk.StringVar, send_button: ttk.Button, error: str | None):
        if error:
            send_button.state(["!disabled"])
            status_var.set(error)
            return
        messagebox.showinfo("Thanks", "Report sent -- thank you for helping improve this.", parent=self)
        self.destroy()


class App(TkinterDnD.Tk):
    def __init__(self):
        super().__init__()
        self.title("Bank Statement -> CSV Converter")
        self.geometry("900x560")
        self.minsize(700, 420)

        self.rows: dict[str, dict] = {}  # treeview iid -> {"date", "description", "amount"}
        self._next_id = 0
        self._last_export_remaining: int | None = None
        self._any_used_ocr = False
        self._last_bank_name = ""

        self._build_toolbar()
        self._build_table()
        self._build_statusbar()
        self._refresh_account_status()
        self._check_for_update_async()

        self.drop_target_register(DND_FILES)
        self.dnd_bind("<<Drop>>", self._on_drop)

    # ---------- self-update ----------

    def _check_for_update_async(self):
        if not updater.is_frozen():
            return  # nothing to swap when running from source

        def worker():
            try:
                info = updater.check_for_update()
            except updater.UpdateError:
                return  # couldn't check (e.g. offline) -- not worth bothering the user
            if info:
                self.after(0, lambda: self._offer_update(info))

        threading.Thread(target=worker, daemon=True).start()

    def _offer_update(self, info: dict):
        message = f"Version {info['version']} is available (you have {updater.APP_VERSION})."
        if info["notes"]:
            message += f"\n\n{info['notes']}"
        message += "\n\nDownload and install it now? The app will restart."
        if messagebox.askyesno("Update available", message):
            self._download_and_apply_update(info["download_url"])

    def _download_and_apply_update(self, download_url: str):
        progress = tk.Toplevel(self)
        progress.title("Updating...")
        progress.resizable(False, False)
        progress.transient(self)
        progress.protocol("WM_DELETE_WINDOW", lambda: None)  # don't let it be closed mid-download
        ttk.Label(progress, text="Downloading update...", padding=12).pack()
        bar = ttk.Progressbar(progress, length=280, mode="indeterminate")
        bar.pack(padx=12, pady=(0, 12))
        bar.start(10)
        progress.grab_set()

        def worker():
            try:
                zip_path = updater.download_update(download_url)
            except updater.UpdateError as exc:
                self.after(0, lambda: self._update_failed(progress, exc))
                return
            self.after(0, lambda: self._finish_update(progress, zip_path))

        threading.Thread(target=worker, daemon=True).start()

    def _update_failed(self, progress: tk.Toplevel, exc: Exception):
        progress.destroy()
        messagebox.showerror("Update failed", str(exc))

    def _finish_update(self, progress: tk.Toplevel, zip_path: str):
        progress.destroy()
        try:
            updater.apply_update_and_restart(zip_path)
        except updater.UpdateError as exc:
            messagebox.showerror("Update failed", str(exc))
            return
        self.destroy()
        os._exit(0)  # hand off to the relauncher script cleanly, skipping normal teardown

    # ---------- UI construction ----------

    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=8)
        bar.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(bar, text="Open PDF Statement(s)...", command=self.open_pdfs).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(bar, text="Select All", command=self.select_all).pack(side=tk.LEFT, padx=6)
        ttk.Button(bar, text="Delete Selected", command=self.delete_selected).pack(side=tk.LEFT, padx=6)
        ttk.Button(bar, text="Flip Sign", command=self.flip_selected_sign).pack(side=tk.LEFT, padx=6)
        ttk.Button(bar, text="Clear All", command=self.clear_all).pack(side=tk.LEFT, padx=6)

        ttk.Label(bar, text="Export date format:").pack(side=tk.LEFT, padx=(24, 4))
        self.date_format_var = tk.StringVar(value="YYYY-MM-DD")
        ttk.Combobox(
            bar,
            textvariable=self.date_format_var,
            values=list(DATE_FORMATS.keys()),
            width=12,
            state="readonly",
        ).pack(side=tk.LEFT)

        ttk.Button(bar, text="Export CSV...", command=self.export_csv).pack(side=tk.RIGHT)
        ttk.Button(bar, text="Report Issue...", command=self.open_report_issue_dialog).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(bar, text="Account", command=self.open_account_dialog).pack(side=tk.RIGHT, padx=(0, 6))
        self.account_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.account_var).pack(side=tk.RIGHT, padx=(0, 6))

    def _build_table(self):
        container = ttk.Frame(self, padding=(8, 0, 8, 8))
        container.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        columns = ("date", "description", "amount", "fee")
        self.tree = EditableTreeview(
            container,
            on_change=self._on_cell_edited,
            columns=columns,
            show="headings",
            selectmode="extended",
        )
        self.tree.heading("date", text="Date")
        self.tree.heading("description", text="Description")
        self.tree.heading("amount", text="Amount")
        self.tree.heading("fee", text="Fee")
        self.tree.column("date", width=120, anchor=tk.W)
        self.tree.column("description", width=460, anchor=tk.W)
        self.tree.column("amount", width=100, anchor=tk.E)
        self.tree.column("fee", width=80, anchor=tk.E)

        vsb = ttk.Scrollbar(container, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # Rows flagged by balance reconciliation as not adding up -- worth a
        # second look before exporting (see reconcile() in parser.py).
        self.tree.tag_configure("mismatch", background="#ffd6d6")

        self.tree.bind("<Control-a>", lambda _e: (self.select_all(), "break")[1])

    def _build_statusbar(self):
        self.status_var = tk.StringVar(
            value="Open, or drag and drop, one or more PDF bank statements to get started."
        )
        ttk.Label(self, textvariable=self.status_var, anchor=tk.W, padding=(8, 4)).pack(side=tk.BOTTOM, fill=tk.X)

    # ---------- account ----------

    def open_account_dialog(self):
        if not license_client.is_configured():
            messagebox.showinfo(
                "Not set up yet",
                "The account backend hasn't been configured yet -- see backend/README.md to set up "
                "Supabase and PayFast. Until then the app works fully, with no sign-in required.",
            )
            return
        AccountDialog(self, on_change=self._refresh_account_status)

    def _refresh_account_status(self):
        if not license_client.is_configured():
            self.account_var.set("")
        elif license_client.is_signed_in():
            self.account_var.set(license_client.current_email() or "Signed in")
        else:
            self.account_var.set("Not signed in")

    def open_upgrade_checkout(self):
        """Write the signed PayFast checkout page to a temp file and open it
        in the user's browser. Payment confirmation flips the account to Pro
        server-side (via the payfast-webhook) within a few seconds; nothing
        else needs to happen in the app itself."""
        try:
            checkout_html = license_client.build_upgrade_checkout_html()
        except license_client.LicenseError as exc:
            messagebox.showerror("Couldn't start checkout", str(exc))
            return

        fd, path = tempfile.mkstemp(suffix=".html", prefix="bsc_upgrade_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(checkout_html)
        webbrowser.open(f"file://{path}")

    # ---------- actions ----------

    def open_pdfs(self):
        paths = filedialog.askopenfilenames(
            title="Select bank statement PDF(s)",
            filetypes=[("PDF files", "*.pdf")],
        )
        if paths:
            self._load_pdfs(paths)

    def _on_drop(self, event):
        paths = self.tk.splitlist(event.data)
        pdf_paths = [p for p in paths if p.lower().endswith(".pdf")]
        skipped = len(paths) - len(pdf_paths)
        if pdf_paths:
            self._load_pdfs(pdf_paths)
        if skipped:
            messagebox.showwarning(
                "Skipped non-PDF file(s)", f"Ignored {skipped} dropped file(s) that weren't PDFs."
            )

    def _extract_with_password_retry(self, path, failed):
        """Try to parse one PDF, prompting for a password (and re-prompting
        on a wrong one) if it's encrypted. Returns an ExtractionResult, or
        None (and appends to failed) if parsing ultimately fails or the
        user cancels."""
        password = None
        wrong_password = False
        while True:
            try:
                return extract_rows(path, password=password)
            except PdfPasswordRequired:
                prompt = (
                    f"{os.path.basename(path)} is password-protected.\nEnter the password:"
                    if not wrong_password
                    else f"Incorrect password for {os.path.basename(path)}. Try again:"
                )
                password = simpledialog.askstring("Password required", prompt, show="*", parent=self)
                if password is None:
                    failed.append(f"{os.path.basename(path)}: password required, skipped")
                    return None
                wrong_password = True
            except Exception as exc:  # pdfplumber/parsing can fail in many ways
                failed.append(f"{os.path.basename(path)}: {exc}")
                return None

    def _load_pdfs(self, paths):
        total_added = 0
        failed = []
        report_lines = []
        any_mismatch = False

        for path in paths:
            result = self._extract_with_password_retry(path, failed)
            if result is None:
                continue

            recon = reconcile(result.rows)
            mismatched_indices = {m.row_index for m in recon.mismatches}
            for i, row in enumerate(result.rows):
                self._add_row(row.date, row.description, row.amount, row.fee, mismatch=i in mismatched_indices)
            total_added += len(result.rows)
            self._any_used_ocr = self._any_used_ocr or result.used_ocr
            self._last_bank_name = os.path.splitext(os.path.basename(path))[0]

            name = os.path.basename(path)
            if not recon.applicable:
                status = "no running balance in this statement to check"
            elif recon.ok:
                status = f"reconciled ({recon.checked} row(s) checked)"
            else:
                any_mismatch = True
                status = f"{len(recon.mismatches)} of {recon.checked} row(s) don't reconcile -- see rows highlighted in red"
            if result.used_ocr:
                status += " -- OCR was used on this file, so results are less certain than a native-text statement"
            report_lines.append(f"{name}: {status}")

        if failed:
            messagebox.showwarning("Some files had issues", "\n".join(failed))

        if report_lines:
            title = "Some statements need review" if any_mismatch else "Reconciliation check"
            dialog = messagebox.showwarning if any_mismatch else messagebox.showinfo
            dialog(title, "\n\n".join(report_lines))

        self.status_var.set(
            f"Added {total_added} transaction(s) from {len(paths)} file(s). "
            f"Review the rows below, fix anything wrong, then export."
        )

    def _add_row(self, date: str, description: str, amount: str, fee: str = "", mismatch: bool = False):
        self._next_id += 1
        iid = f"row{self._next_id}"
        tags = ("mismatch",) if mismatch else ()
        self.tree.insert("", tk.END, iid=iid, values=(date, description, amount, fee), tags=tags)
        self.rows[iid] = {"date": date, "description": description, "amount": amount, "fee": fee}

    def _on_cell_edited(self, row_id: str, column: str, new_value: str):
        if row_id in self.rows:
            self.rows[row_id][column] = new_value

    def select_all(self):
        self.tree.selection_set(self.tree.get_children())

    def delete_selected(self):
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.rows.pop(iid, None)

    def flip_selected_sign(self):
        for iid in self.tree.selection():
            try:
                value = float(self.rows[iid]["amount"])
            except (KeyError, ValueError):
                continue
            new_value = f"{-value:.2f}"
            self.rows[iid]["amount"] = new_value
            self.tree.set(iid, "amount", new_value)

    def clear_all(self):
        if not self.rows:
            return
        if messagebox.askyesno("Clear all", "Remove every parsed row?"):
            self.tree.delete(*self.tree.get_children())
            self.rows.clear()
            self._any_used_ocr = False
            self._last_bank_name = ""
            self.status_var.set("Cleared. Open one or more PDF bank statements to get started.")

    def open_report_issue_dialog(self):
        if not license_client.is_configured():
            messagebox.showinfo(
                "Not set up yet",
                "The account backend hasn't been configured yet, so there's nowhere to send a "
                "report to -- see backend/README.md.",
            )
            return
        ReportIssueDialog(self)

    def _check_usage_allows_export(self) -> bool:
        """Gate point for the subscription model. Returns True if the export
        should proceed. A no-op (always True) until the backend is configured
        -- see backend/README.md.
        """
        if not license_client.is_configured():
            return True

        if not license_client.is_signed_in():
            if not messagebox.askyesno(
                "Sign in required",
                "Sign in (or create a free account) to export.\nFree tier includes "
                f"{license_client.FREE_TIER_LIMIT} exports/month.",
            ):
                return False
            AccountDialog(self, on_change=self._refresh_account_status)
            if not license_client.is_signed_in():
                return False

        try:
            result = license_client.check_and_increment_usage()
        except license_client.LicenseError as exc:
            messagebox.showerror(
                "Couldn't verify your account",
                f"{exc}\n\nCheck your internet connection and try again.",
            )
            return False

        if not result.get("allowed"):
            if messagebox.askyesno(
                "Free tier limit reached",
                f"You've used all {license_client.FREE_TIER_LIMIT} free exports this month.\n\n"
                "Upgrade to Pro for unlimited exports?",
            ):
                self.open_upgrade_checkout()
            return False

        self._last_export_remaining = result.get("remaining")
        return True

    def export_csv(self):
        if not self.rows:
            messagebox.showinfo("Nothing to export", "There are no transactions to export yet.")
            return

        out_path = filedialog.asksaveasfilename(
            title="Save CSV as",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
        )
        if not out_path:
            return

        # Only spend a usage credit once the user has actually committed to
        # exporting (chosen a save location) -- not just for clicking the button.
        if not self._check_usage_allows_export():
            return

        strftime_fmt = DATE_FORMATS[self.date_format_var.get()]
        unparsed_dates = []

        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Date", "Description", "Amount", "Fee"])
            for iid in self.tree.get_children():
                row = self.rows[iid]
                parsed = parse_date(row["date"])
                if parsed:
                    date_out = parsed.strftime(strftime_fmt)
                else:
                    date_out = row["date"]
                    unparsed_dates.append(date_out)
                try:
                    amount_out = f"{float(row['amount']):.2f}"
                except ValueError:
                    amount_out = row["amount"]
                fee_raw = row.get("fee", "")
                try:
                    fee_out = f"{float(fee_raw):.2f}" if fee_raw else ""
                except ValueError:
                    fee_out = fee_raw
                writer.writerow([date_out, row["description"], amount_out, fee_out])

        msg = f"Saved {len(self.rows)} transaction(s) to:\n{out_path}"
        if unparsed_dates:
            msg += f"\n\n{len(unparsed_dates)} row(s) had a date that couldn't be reformatted and were left as-is."
        messagebox.showinfo("Export complete", msg)
        status = f"Exported {len(self.rows)} row(s) to {os.path.basename(out_path)}."
        if self._last_export_remaining is not None:
            status += f" {self._last_export_remaining} free export(s) left this month."
        self.status_var.set(status)


if __name__ == "__main__":
    App().mainloop()
