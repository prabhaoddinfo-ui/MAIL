"""
main.py - Main Streamlit Dashboard
Combined version with all features
"""
import streamlit as st
import pandas as pd
import time
import socket
import smtplib
from email.message import EmailMessage
from concurrent.futures import ThreadPoolExecutor, as_completed
import tempfile
import os
import sys

# Import our modules
try:
    from validator_core import process_one, load_disposable_set
    from send_mail import send_one_email, test_smtp_connection
except ImportError as e:
    st.error(f"Import error: {e}. Make sure validator_core.py and send_mail.py are in the same directory.")
    st.stop()

# Page config
st.set_page_config(
    page_title="Email Validator & Automation",
    page_icon="📧",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS
st.markdown("""
<style>
    .main-header {
        font-size: 2.5rem;
        color: #1f77b4;
        margin-bottom: 1rem;
    }
    .metric-card {
        background-color: #f0f2f6;
        padding: 1rem;
        border-radius: 0.5rem;
        border-left: 4px solid #1f77b4;
    }   
    .success { color: #28a745; }
    .error { color: #dc3545; }
    .warning { color: #ffc107; }
</style>
""", unsafe_allow_html=True)

# Session state initialization
if 'validated_data' not in st.session_state:
    st.session_state.validated_data = None
if 'send_results' not in st.session_state:
    st.session_state.send_results = None
if 'smtp_config' not in st.session_state:
    st.session_state.smtp_config = {
        'host': '',
        'port': 587,
        'username': '',
        'password': '',
        'from_addr': 'verify@example.com',
        'use_ssl': False,
        'use_tls': True
    }

def safe_merge(original_df, results_df, suffix="_validated"):
    """Merge dataframes safely avoiding column name conflicts"""
    conflicts = set(original_df.columns).intersection(results_df.columns)
    if conflicts:
        rename_map = {c: f"{c}{suffix}" for c in conflicts}
        results_df = results_df.rename(columns=rename_map)
    return pd.concat([original_df.reset_index(drop=True), 
                     results_df.reset_index(drop=True)], axis=1)

def main():
    # Header
    st.markdown('<h1 class="main-header">📧 Email Validator & Automation Dashboard</h1>', 
                unsafe_allow_html=True)
    st.markdown("---")
    
    # Sidebar
    with st.sidebar:
        st.header("Settings")
        
        # Validation settings
        st.subheader("Validation Settings")
        do_smtp_check = st.checkbox("Enable SMTP Check", value=False, 
                                   help="Perform real SMTP verification (slower)")
        validation_timeout = st.slider("Timeout (seconds)", 2, 30, 8)
        validation_workers = st.slider("Threads", 1, 20, 8)
        
        # SMTP Configuration
        st.subheader("SendGrid Configuration")
        smtp_host = st.text_input("SMTP Host (unused — SendGrid only)", 
                                 value=st.session_state.smtp_config['host'],
                                 disabled=True)
        smtp_port = st.number_input("Port (unused)", value=587, min_value=1, max_value=65535,
                                    disabled=True)
        smtp_user = st.text_input("From Email Address", 
                                 value=st.session_state.smtp_config['username'],
                                 help="Must be a verified sender in your SendGrid account")
        smtp_pass = st.text_input("SendGrid API Key", type="password",
                                 value=st.session_state.smtp_config['password'],
                                 help="Get free key at sendgrid.com (100 emails/day free). Or set SENDGRID_API_KEY env var on Render.")
        from_addr = st.text_input("From Address", 
                                 value=st.session_state.smtp_config['from_addr'],
                                 help="Must match a verified sender in SendGrid")
        use_ssl = st.checkbox("Use SSL", value=False, disabled=True)
        use_tls = st.checkbox("Use TLS", value=True, disabled=True)
        
        # Save SMTP config
        if st.button("Save SMTP Config"):
            st.session_state.smtp_config.update({
                'host': smtp_host,
                'port': smtp_port,
                'username': smtp_user,
                'password': smtp_pass,
                'from_addr': from_addr,
                'use_ssl': use_ssl,
                'use_tls': use_tls
            })
            st.success("SMTP configuration saved!")
        
        # Test connection
        if st.button("Test SendGrid API Key"):
            with st.spinner("Testing SendGrid API key..."):
                success, message = test_smtp_connection(
                    host="", port=587, username=smtp_user, password=smtp_pass
                )
                if success:
                    st.success(f"✅ {message}")
                else:
                    st.error(f"❌ {message}")
        
        st.info("💡 **Render free tier blocks SMTP ports.**\n\nThis app uses [SendGrid](https://sendgrid.com) (free: 100 emails/day).\n\n1. Sign up at sendgrid.com\n2. Create an API key\n3. Verify your sender email\n4. Paste the key above")
    
    # Main content - Tabs
    tab1, tab2, tab3, tab4 = st.tabs([
        "📤 Upload & Validate", 
        "✅ Results", 
        "✉️ Send Emails", 
        "📊 Reports"
    ])
    
    # Tab 1: Upload & Validate
    with tab1:
        st.header("Upload & Validate Emails")
        
        col1, col2 = st.columns(2)
        with col1:
            uploaded_file = st.file_uploader(
                "Upload CSV/Excel/TXT",
                type=["csv", "xlsx", "xls", "txt"],
                help="Upload file containing email addresses"
            )
        
        with col2:
            email_col = st.text_input("Email Column Name", value="email")
            disposable_file = st.text_input("Disposable Domains File (optional)", "")
        
        if uploaded_file:
            # Read file
            try:
                if uploaded_file.name.endswith('.csv'):
                    df = pd.read_csv(uploaded_file, dtype=str)
                elif uploaded_file.name.endswith(('.xlsx', '.xls')):
                    df = pd.read_excel(uploaded_file, dtype=str)
                elif uploaded_file.name.endswith('.txt'):
                    # Read as text file with one email per line
                    content = uploaded_file.read().decode('utf-8')
                    emails = [line.strip() for line in content.split('\n') if line.strip()]
                    df = pd.DataFrame({'email': emails})
                else:
                    st.error(f"Unsupported file type: {uploaded_file.name}")
                    return
                
                st.success(f"Loaded {len(df)} rows from {uploaded_file.name}")
                
                if email_col not in df.columns:
                    st.error(f"Column '{email_col}' not found. Available columns: {list(df.columns)}")
                else:
                    # Show preview
                    with st.expander("Preview Data"):
                        st.dataframe(df.head(10), use_container_width=True)
                    
                    # Validate button
                    if st.button("🚀 Start Validation", type="primary"):
                        with st.spinner(f"Validating {len(df)} emails..."):
                            emails_list = df[email_col].fillna("").astype(str).tolist()
                            disposable_set = load_disposable_set(disposable_file) if disposable_file else None
                            
                            results = [None] * len(emails_list)
                            
                            with ThreadPoolExecutor(max_workers=validation_workers) as executor:
                                futures = {
                                    executor.submit(
                                        process_one,
                                        email,
                                        disposable_set,
                                        do_smtp_check,
                                        validation_timeout,
                                        from_addr or "verify@example.com"
                                    ): idx
                                    for idx, email in enumerate(emails_list)
                                }
                                
                                progress_bar = st.progress(0)
                                status_text = st.empty()
                                
                                for i, future in enumerate(as_completed(futures)):
                                    idx = futures[future]
                                    try:
                                        results[idx] = future.result()
                                    except Exception as e:
                                        results[idx] = {
                                            "email": emails_list[idx],
                                            "email_type": "Error",
                                            "reason": str(e)
                                        }
                                    progress = (i + 1) / len(futures)
                                    progress_bar.progress(progress)
                                    status_text.text(f"Processed {i + 1}/{len(futures)} emails")
                            
                            # Create results dataframe
                            results_df = pd.DataFrame(results)
                            final_df = safe_merge(df, results_df)
                            
                            # Store in session state
                            st.session_state.validated_data = {
                                'df': final_df,
                                'filename': uploaded_file.name,
                                'original_df': df,
                                'email_col': email_col
                            }
                            
                            st.success("✅ Validation complete!")
                            st.rerun()
            
            except Exception as e:
                st.error(f"Error processing file: {str(e)}")
    
    # Tab 2: Results
    with tab2:
        st.header("Validation Results")
        
        if st.session_state.validated_data:
            data = st.session_state.validated_data
            df = data['df']
            
            # Stats
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                valid_count = len(df[df.get('email_type') == 'Valid']) if 'email_type' in df.columns else 0
                st.metric("✅ Valid", valid_count)
            with col2:
                invalid_count = len(df[df.get('email_type') == 'Invalid']) if 'email_type' in df.columns else 0
                st.metric("❌ Invalid", invalid_count)
            with col3:
                risky_count = len(df[df.get('email_type') == 'Risky']) if 'email_type' in df.columns else 0
                st.metric("⚠️ Risky", risky_count)
            with col4:
                mx_count = len(df[df.get('has_mx') == True]) if 'has_mx' in df.columns else 0
                st.metric("📡 MX Records", mx_count)
            
            # Filters
            st.subheader("Filters")
            filter_col1, filter_col2, filter_col3 = st.columns(3)
            with filter_col1:
                show_valid = st.checkbox("Show Valid", value=True)
            with filter_col2:
                show_invalid = st.checkbox("Show Invalid", value=True)
            with filter_col3:
                show_risky = st.checkbox("Show Risky", value=True)
            
            # Apply filters
            filtered_df = df.copy()
            conditions = []
            if show_valid and 'email_type' in filtered_df.columns:
                conditions.append(filtered_df['email_type'] == 'Valid')
            if show_invalid and 'email_type' in filtered_df.columns:
                conditions.append(filtered_df['email_type'] == 'Invalid')
            if show_risky and 'email_type' in filtered_df.columns:
                conditions.append(filtered_df['email_type'] == 'Risky')
            
            if conditions:
                filtered_df = filtered_df[pd.concat(conditions, axis=1).any(axis=1)]
            
            # Display table
            st.dataframe(filtered_df, use_container_width=True, height=400)
            
            # Download button
            csv = filtered_df.to_csv(index=False)
            st.download_button(
                label="📥 Download Results (CSV)",
                data=csv,
                file_name=f"validated_{data['filename']}",
                mime="text/csv"
            )
        else:
            st.info("No validation results yet. Upload and validate a file first.")
    
    # Tab 3: Send Emails
    with tab3:
        st.header("Send Automated Replies")
        
        if not st.session_state.validated_data:
            st.warning("Please validate emails first in the Upload & Validate tab.")
        else:
            data = st.session_state.validated_data
            
            col1, col2 = st.columns(2)
            with col1:
                st.subheader("Email Content")
                subject = st.text_input("Subject", value=f"Re: {data['filename']}")
                body = st.text_area("Body", height=200, value="""Hello,

This is an automated reply to your message.

Best regards,
The Team""")
            
            with col2:
                st.subheader("Send Options")
                only_valid = st.checkbox("Send only to Valid emails", value=True)
                dry_run = st.checkbox("Dry Run (Test without sending)", value=True)
                send_workers = st.slider("Concurrent Workers", 1, 10, 3)
                send_rate = st.number_input("Rate limit (seconds)", 0.0, 10.0, 0.0, 0.1)
                max_retries = st.number_input("Max Retries", 1, 10, 3)
            
            # Get recipients
            if only_valid:
                recipients_df = data['df'][data['df']['email_type'] == 'Valid']
            else:
                recipients_df = data['df'][data['df']['email_type'].isin(['Valid', 'Risky'])]
            
            recipients = recipients_df[data['email_col']].dropna().unique().tolist()
            
            st.info(f"📧 {len(recipients)} recipients selected")
            
            if st.button("🚀 Send Emails", type="primary"):
                if not recipients:
                    st.warning("No recipients to send to.")
                elif not dry_run and not st.session_state.smtp_config['host']:
                    st.warning("Please configure SMTP settings in the sidebar.")
                else:
                    # Send emails
                    progress_bar = st.progress(0)
                    status_text = st.empty()
                    
                    results = []
                    
                    with ThreadPoolExecutor(max_workers=send_workers) as executor:
                        futures = {}
                        for recipient in recipients:
                            future = executor.submit(
                                send_one_email,
                                smtp_host=st.session_state.smtp_config['host'],
                                smtp_port=st.session_state.smtp_config['port'],
                                smtp_user=st.session_state.smtp_config['username'],
                                smtp_pass=st.session_state.smtp_config['password'],
                                use_ssl=st.session_state.smtp_config['use_ssl'],
                                use_tls=st.session_state.smtp_config['use_tls'],
                                from_addr=st.session_state.smtp_config['from_addr'],
                                to_addr=recipient,
                                subject=subject,
                                body=body,
                                timeout=30,
                                max_retries=max_retries,
                                dry_run=dry_run
                            )
                            futures[future] = recipient
                        
                        for i, future in enumerate(as_completed(futures)):
                            recipient = futures[future]
                            try:
                                result = future.result()
                            except Exception as e:
                                result = {
                                    'to': recipient,
                                    'status': 'failed',
                                    'error': str(e),
                                    'attempts': 0
                                }
                            
                            results.append(result)
                            progress = (i + 1) / len(futures)
                            progress_bar.progress(progress)
                            status_text.text(f"Sent {i + 1}/{len(futures)}: {recipient} ({result['status']})")
                            
                            if send_rate > 0:
                                time.sleep(send_rate)
                    
                    # Store results
                    st.session_state.send_results = pd.DataFrame(results)
                    
                    # Show summary
                    st.success("✅ Send operation completed!")
                    
                    # Results summary
                    if 'status' in st.session_state.send_results.columns:
                        sent = len(st.session_state.send_results[st.session_state.send_results['status'] == 'sent'])
                        failed = len(st.session_state.send_results[st.session_state.send_results['status'] == 'failed'])
                        dry = len(st.session_state.send_results[st.session_state.send_results['status'] == 'dry-run'])
                        
                        col1, col2, col3 = st.columns(3)
                        with col1:
                            st.metric("✅ Sent", sent)
                        with col2:
                            st.metric("❌ Failed", failed)
                        with col3:
                            st.metric("📝 Dry Run", dry)
                    
                    # Download results
                    if st.session_state.send_results is not None:
                        csv = st.session_state.send_results.to_csv(index=False)
                        st.download_button(
                            label="📥 Download Send Results",
                            data=csv,
                            file_name="send_results.csv",
                            mime="text/csv"
                        )
    
    # Tab 4: Reports
    with tab4:
        st.header("Reports & Analytics")
        
        if st.session_state.validated_data:
            df = st.session_state.validated_data['df']
            
            col1, col2 = st.columns(2)
            
            with col1:
                st.subheader("Validation Statistics")
                if 'email_type' in df.columns:
                    chart_data = df['email_type'].value_counts()
                    st.bar_chart(chart_data)
            
            with col2:
                st.subheader("Domain Analysis")
                if 'domain' in df.columns:
                    top_domains = df['domain'].value_counts().head(10)
                    st.dataframe(top_domains)
            
            # Detailed report
            st.subheader("Detailed Report")
            report_cols = []
            if 'email_type' in df.columns:
                report_cols.append('email_type')
            if 'has_mx' in df.columns:
                report_cols.append('has_mx')
            if 'disposable' in df.columns:
                report_cols.append('disposable')
            if 'smtp_status' in df.columns:
                report_cols.append('smtp_status')
            
            if report_cols:
                for col in report_cols:
                    if col in df.columns:
                        st.write(f"**{col}:**")
                        st.write(df[col].value_counts())
                        st.write("---")
        else:
            st.info("No data available for reports. Please validate emails first.")

if __name__ == "__main__":
    main()