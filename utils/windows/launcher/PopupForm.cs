using System;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace AseStudio.Launcher
{
    /// <summary>A secondary window for window.open() targets inside ASE
    /// Studio itself (e.g. the memory pop-out). It shares the main window's
    /// CoreWebView2Environment, so BroadcastChannel keeps it in sync, same
    /// as launcher.m's createWebViewWithConfiguration on macOS.</summary>
    internal sealed class PopupForm : Form
    {
        public WebView2 Web { get; }

        public PopupForm(int width, int height)
        {
            Text = "ASE Studio";
            Width = width > 0 ? width : 1050;
            Height = height > 0 ? height : 720;
            MinimumSize = new System.Drawing.Size(650, 400);
            StartPosition = FormStartPosition.CenterScreen;

            Web = new WebView2 { Dock = DockStyle.Fill };
            Controls.Add(Web);
        }

        protected override void OnFormClosed(FormClosedEventArgs e)
        {
            base.OnFormClosed(e);
            Web.Dispose();
        }
    }
}
