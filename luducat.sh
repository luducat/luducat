#!/usr/bin/env bash
# luducat - Cross-Platform Game Catalogue Browser
# Launcher script with automatic environment setup
#
# This script:
# - Checks for Python 3 and virtual environment support
# - Creates a virtual environment if it doesn't exist
# - Installs/updates required Python packages
# - Launches the application
# - Provides user-friendly error messages

set -uo pipefail

# ============================================================================
# Configuration
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
REQUIREMENTS_FILE="$SCRIPT_DIR/requirements.txt"
APP_NAME="luducat"
APP_ID="com.luducat.luducat"  # GNOME App ID / Desktop file name
SUPPORT_URL="https://github.com/luducat/luducat/issues"
MIN_PYTHON_VERSION="3.10"

# Desktop integration paths
XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
ICONS_DIR="$XDG_DATA_HOME/icons/hicolor"
APPLICATIONS_DIR="$XDG_DATA_HOME/applications"
ICONS_MARKER="$SCRIPT_DIR/.icons-installed"

# ============================================================================
# Helper Functions
# ============================================================================

# Colors for terminal output (if supported)
if [[ -t 1 ]] && command -v tput >/dev/null 2>&1; then
    RED=$(tput setaf 1)
    GREEN=$(tput setaf 2)
    YELLOW=$(tput setaf 3)
    BLUE=$(tput setaf 4)
    BOLD=$(tput bold)
    RESET=$(tput sgr0)
else
    RED=""
    GREEN=""
    YELLOW=""
    BLUE=""
    BOLD=""
    RESET=""
fi

log_info() {
    echo "${BLUE}[INFO]${RESET} $*"
}

log_success() {
    echo "${GREEN}[OK]${RESET} $*"
}

log_warning() {
    echo "${YELLOW}[WARNING]${RESET} $*"
}

log_error() {
    echo "${RED}[ERROR]${RESET} $*" >&2
}

# Show error with user-friendly instructions
show_error_and_exit() {
    local error_type="$1"
    local details="$2"

    echo
    echo "${RED}${BOLD}============================================================${RESET}"
    echo "${RED}${BOLD}  $APP_NAME could not start${RESET}"
    echo "${RED}${BOLD}============================================================${RESET}"
    echo
    echo "${BOLD}What went wrong:${RESET}"
    echo "  $details"
    echo

    case "$error_type" in
        python_missing)
            echo "${BOLD}How to fix this:${RESET}"
            echo
            if [[ -n "${DISTRO_ID:-}" ]]; then
                case "$DISTRO_ID" in
                    debian)
                        echo "  Run this command in your terminal:"
                        echo "    ${BOLD}sudo apt update && sudo apt install python3${RESET}"
                        ;;
                    ubuntu|linuxmint|pop)
                        echo "  Run this command in your terminal:"
                        echo "    ${BOLD}sudo apt update && sudo apt install python3${RESET}"
                        ;;
                    fedora)
                        echo "  Run this command in your terminal:"
                        echo "    ${BOLD}sudo dnf install python3${RESET}"
                        ;;
                    opensuse*)
                        echo "  Run this command in your terminal:"
                        echo "    ${BOLD}sudo zypper install python3${RESET}"
                        ;;
                    arch|manjaro)
                        echo "  Run this command in your terminal:"
                        echo "    ${BOLD}sudo pacman -S python${RESET}"
                        ;;
                    *)
                        echo "  Install Python 3 using your distribution's package manager."
                        ;;
                esac
            else
                echo "  Install Python 3 using your distribution's package manager."
            fi
            ;;

        python_version)
            echo "${BOLD}How to fix this:${RESET}"
            echo
            echo "  $APP_NAME requires Python $MIN_PYTHON_VERSION or newer."
            echo "  Your system has an older version of Python installed."
            echo
            if [[ -n "${DISTRO_ID:-}" ]]; then
                case "$DISTRO_ID" in
                    debian|ubuntu|linuxmint|pop)
                        echo "  Options:"
                        echo "  1. Upgrade your system to a newer version"
                        echo "  2. Install Python from the 'deadsnakes' PPA (advanced)"
                        ;;
                    *)
                        echo "  Please upgrade your system or install a newer Python version."
                        ;;
                esac
            fi
            ;;

        venv_missing)
            echo "${BOLD}How to fix this:${RESET}"
            echo
            if [[ -n "${DISTRO_ID:-}" ]]; then
                case "$DISTRO_ID" in
                    debian)
                        echo "  Run this command in your terminal:"
                        echo "    ${BOLD}sudo apt update && sudo apt install python3-venv${RESET}"
                        ;;
                    ubuntu|linuxmint|pop)
                        echo "  Run this command in your terminal:"
                        echo "    ${BOLD}sudo apt update && sudo apt install python3-venv${RESET}"
                        ;;
                    fedora)
                        echo "  Run this command in your terminal:"
                        echo "    ${BOLD}sudo dnf install python3-virtualenv${RESET}"
                        ;;
                    opensuse*)
                        echo "  Run this command in your terminal:"
                        echo "    ${BOLD}sudo zypper install python3-virtualenv${RESET}"
                        ;;
                    arch|manjaro)
                        echo "  The venv module should be included with Python."
                        echo "  Try reinstalling Python:"
                        echo "    ${BOLD}sudo pacman -S python${RESET}"
                        ;;
                    *)
                        echo "  Install the Python venv module using your package manager."
                        echo "  Look for a package named 'python3-venv' or 'python3-virtualenv'."
                        ;;
                esac
            else
                echo "  Install the Python venv module using your package manager."
                echo "  Common package names: python3-venv, python3-virtualenv"
            fi
            ;;

        pip_missing)
            echo "${BOLD}How to fix this:${RESET}"
            echo
            if [[ -n "${DISTRO_ID:-}" ]]; then
                case "$DISTRO_ID" in
                    debian|ubuntu|linuxmint|pop)
                        echo "  Run this command in your terminal:"
                        echo "    ${BOLD}sudo apt update && sudo apt install python3-pip${RESET}"
                        ;;
                    fedora)
                        echo "  Run this command in your terminal:"
                        echo "    ${BOLD}sudo dnf install python3-pip${RESET}"
                        ;;
                    opensuse*)
                        echo "  Run this command in your terminal:"
                        echo "    ${BOLD}sudo zypper install python3-pip${RESET}"
                        ;;
                    arch|manjaro)
                        echo "  Run this command in your terminal:"
                        echo "    ${BOLD}sudo pacman -S python-pip${RESET}"
                        ;;
                    *)
                        echo "  Install pip using your package manager."
                        ;;
                esac
            fi
            ;;

        venv_create_failed)
            echo "${BOLD}How to fix this:${RESET}"
            echo
            echo "  The virtual environment could not be created."
            echo "  Try these steps:"
            echo "  1. Make sure you have write permission to: $SCRIPT_DIR"
            echo "  2. Delete any existing broken .venv folder:"
            echo "     ${BOLD}rm -rf \"$VENV_DIR\"${RESET}"
            echo "  3. Run this script again"
            ;;

        pip_install_failed)
            echo "${BOLD}How to fix this:${RESET}"
            echo
            echo "  The required Python packages could not be installed."
            echo "  This might be a temporary network issue."
            echo
            echo "  Try these steps:"
            echo "  1. Check your internet connection"
            echo "  2. Run the script again"
            echo "  3. If it still fails, try manually:"
            echo "     ${BOLD}source \"$VENV_DIR/bin/activate\"${RESET}"
            echo "     ${BOLD}pip install -r \"$REQUIREMENTS_FILE\"${RESET}"
            ;;

        requirements_missing)
            echo "${BOLD}How to fix this:${RESET}"
            echo
            echo "  The file requirements.txt is missing from the installation."
            echo "  This suggests an incomplete or corrupted installation."
            echo
            echo "  Please re-download $APP_NAME from the official source."
            ;;

        *)
            echo "${BOLD}How to fix this:${RESET}"
            echo
            echo "  An unexpected error occurred."
            ;;
    esac

    echo
    echo "${BOLD}Need help?${RESET}"
    echo "  If you're still having trouble, please report this issue."
    echo "  Copy everything above and create a report at:"
    echo "    ${BLUE}$SUPPORT_URL${RESET}"
    echo
    echo "  Please include:"
    echo "  - The full error message above"
    echo "  - Your Linux distribution and version"
    echo "  - The output of: ${BOLD}python3 --version${RESET}"
    echo

    exit 1
}

# ============================================================================
# System Detection
# ============================================================================

detect_distro() {
    # Try /etc/os-release first (standard), then /usr/lib/os-release (fallback)
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
    elif [[ -r /usr/lib/os-release ]]; then
        # shellcheck disable=SC1091
        . /usr/lib/os-release
    else
        # Unknown distro, but we can still try to continue
        DISTRO_ID="unknown"
        DISTRO_PRETTY_NAME="Unknown Linux"
        return
    fi

    DISTRO_ID="${ID:-unknown}"
    DISTRO_VERSION_ID="${VERSION_ID:-}"
    DISTRO_PRETTY_NAME="${PRETTY_NAME:-Linux}"

    # Normalize some IDs for easier handling
    # ID_LIKE can contain multiple space-separated values
    case "$DISTRO_ID" in
        linuxmint|pop|elementary|zorin|neon)
            # These are Ubuntu-based
            DISTRO_FAMILY="ubuntu"
            ;;
        *)
            if [[ "${ID_LIKE:-}" == *"ubuntu"* ]]; then
                DISTRO_FAMILY="ubuntu"
            elif [[ "${ID_LIKE:-}" == *"debian"* ]]; then
                DISTRO_FAMILY="debian"
            elif [[ "${ID_LIKE:-}" == *"fedora"* ]]; then
                DISTRO_FAMILY="fedora"
            elif [[ "${ID_LIKE:-}" == *"arch"* ]]; then
                DISTRO_FAMILY="arch"
            elif [[ "${ID_LIKE:-}" == *"suse"* ]]; then
                DISTRO_FAMILY="suse"
            else
                DISTRO_FAMILY="$DISTRO_ID"
            fi
            ;;
    esac

    export DISTRO_ID DISTRO_VERSION_ID DISTRO_PRETTY_NAME DISTRO_FAMILY
}

# ============================================================================
# Python Checks
# ============================================================================

check_python() {
    log_info "Checking Python installation..."

    # Check if python3 exists
    if ! command -v python3 >/dev/null 2>&1; then
        show_error_and_exit "python_missing" "Python 3 is not installed on your system."
    fi

    # Get Python version
    PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PYTHON_FULL_VERSION=$(python3 --version 2>&1)

    log_info "Found: $PYTHON_FULL_VERSION"

    # Check minimum version
    if ! python3 -c "import sys; exit(0 if sys.version_info >= (${MIN_PYTHON_VERSION//./, }) else 1)" 2>/dev/null; then
        show_error_and_exit "python_version" \
            "Python $PYTHON_VERSION is installed, but $APP_NAME requires Python $MIN_PYTHON_VERSION or newer."
    fi

    log_success "Python version $PYTHON_VERSION meets requirements"
}

check_venv_support() {
    log_info "Checking virtual environment support..."

    # Check if venv module is available
    if ! python3 -c "import venv" 2>/dev/null; then
        show_error_and_exit "venv_missing" \
            "The Python 'venv' module is not installed. This is needed to create an isolated environment for $APP_NAME."
    fi

    # Also verify it can actually create a venv (some systems have broken venv)
    if ! python3 -m venv --help >/dev/null 2>&1; then
        show_error_and_exit "venv_missing" \
            "The Python 'venv' module is installed but not working correctly."
    fi

    log_success "Virtual environment support is available"
}

# ============================================================================
# Virtual Environment Setup
# ============================================================================

setup_venv() {
    if [[ -d "$VENV_DIR" ]] && [[ -f "$VENV_DIR/bin/activate" ]]; then
        log_success "Virtual environment exists at $VENV_DIR"
        return 0
    fi

    log_info "Creating virtual environment..."

    # Remove any broken/partial venv
    if [[ -e "$VENV_DIR" ]]; then
        log_warning "Removing incomplete virtual environment..."
        rm -rf "$VENV_DIR"
    fi

    # Create the virtual environment
    if ! python3 -m venv "$VENV_DIR" 2>&1; then
        show_error_and_exit "venv_create_failed" \
            "Failed to create virtual environment at $VENV_DIR"
    fi

    log_success "Virtual environment created successfully"
}

# ============================================================================
# Package Management
# ============================================================================

check_and_install_packages() {
    log_info "Checking Python packages..."

    # Verify requirements.txt exists
    if [[ ! -f "$REQUIREMENTS_FILE" ]]; then
        show_error_and_exit "requirements_missing" \
            "The file requirements.txt is missing from $SCRIPT_DIR"
    fi

    # Activate the virtual environment
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"

    # Upgrade pip first (quietly, ignore errors as old pip still works)
    pip install --quiet --upgrade pip 2>/dev/null || true

    # Check if all packages are installed and up to date
    local needs_install=false

    # Quick check: see if we can import the main module
    if ! python -c "import PySide6" 2>/dev/null; then
        needs_install=true
        log_info "Required packages need to be installed..."
    else
        # Check if any packages are outdated compared to requirements
        # Use pip's dry-run to see if anything would be installed/upgraded
        if pip install --dry-run -r "$REQUIREMENTS_FILE" 2>&1 | grep -q "Would install"; then
            needs_install=true
            log_info "Some packages need to be updated..."
        fi
    fi

    if [[ "$needs_install" == "true" ]]; then
        log_info "Installing/updating packages (this may take a moment)..."

        # Install with progress indication
        if ! pip install --upgrade -r "$REQUIREMENTS_FILE" 2>&1 | while IFS= read -r line; do
            # Show dots for progress but hide verbose pip output
            if [[ "$line" == *"Successfully"* ]] || [[ "$line" == *"Requirement already"* ]]; then
                :  # Skip these lines
            elif [[ "$line" == *"Installing"* ]] || [[ "$line" == *"Collecting"* ]]; then
                echo -n "."
            fi
        done; then
            echo  # Newline after dots
            deactivate 2>/dev/null || true
            show_error_and_exit "pip_install_failed" \
                "Failed to install required Python packages."
        fi
        echo  # Newline after dots

        log_success "All packages installed successfully"
    else
        log_success "All packages are up to date"
    fi

    # Don't deactivate here - we'll use this environment to run the app
}

# ============================================================================
# Desktop Integration (GNOME/KDE icon support)
# ============================================================================

install_desktop_integration() {
    # Install icons and desktop file for proper desktop environment integration
    # This enables the app icon to appear in GNOME dock, KDE panel, etc.

    local force_install="${1:-false}"
    local app_icons_dir="$SCRIPT_DIR/$APP_NAME/assets/appicons"

    # Check if already installed (unless forced)
    if [[ -f "$ICONS_MARKER" ]] && [[ "$force_install" != "true" ]]; then
        return 0
    fi

    # Check if icon assets exist
    if [[ ! -d "$app_icons_dir" ]]; then
        log_warning "Icon assets not found at $app_icons_dir, skipping desktop integration"
        return 0
    fi

    log_info "Installing desktop integration..."

    # Install PNG icons to hicolor theme
    local sizes=(16 24 32 48 64 128 256 512)
    for size in "${sizes[@]}"; do
        local src="$app_icons_dir/app_icon_${size}x${size}.png"
        local dest_dir="$ICONS_DIR/${size}x${size}/apps"
        local dest="$dest_dir/${APP_ID}.png"

        if [[ -f "$src" ]]; then
            mkdir -p "$dest_dir"
            cp "$src" "$dest"
        fi
    done

    # Install SVG to scalable (if available)
    local svg_src="$app_icons_dir/app_icon.svg"
    if [[ -f "$svg_src" ]]; then
        local svg_dest_dir="$ICONS_DIR/scalable/apps"
        mkdir -p "$svg_dest_dir"
        cp "$svg_src" "$svg_dest_dir/${APP_ID}.svg"
    fi

    # Install desktop file
    local desktop_src="$SCRIPT_DIR/${APP_ID}.desktop"
    if [[ -f "$desktop_src" ]]; then
        mkdir -p "$APPLICATIONS_DIR"
        # Update Exec path to point to actual script location
        sed "s|Exec=luducat.sh|Exec=$SCRIPT_DIR/luducat.sh|" "$desktop_src" > "$APPLICATIONS_DIR/${APP_ID}.desktop"
    fi

    # Update icon cache (if gtk-update-icon-cache is available)
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -f -t "$ICONS_DIR" 2>/dev/null || true
    fi

    # Update desktop database (if update-desktop-database is available)
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$APPLICATIONS_DIR" 2>/dev/null || true
    fi

    # Create marker file
    touch "$ICONS_MARKER"

    log_success "Desktop integration installed"
}

# ============================================================================
# Application Launch
# ============================================================================

launch_app() {
    log_info "Starting $APP_NAME..."
    echo

    cd "$SCRIPT_DIR"

    # The venv should already be activated from check_and_install_packages
    # But activate again just to be safe
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"

    # Limit glibc memory arenas to reduce fragmentation from multi-threaded
    # QPixmap allocation/deallocation (worker threads vs main thread).
    export MALLOC_ARENA_MAX=2

    # Launch the application
    # Pass through any command line arguments
    python -m luducat "$@"

    # Capture exit code
    local exit_code=$?

    # Deactivate virtual environment
    deactivate 2>/dev/null || true

    return $exit_code
}

# ============================================================================
# Main Script
# ============================================================================

main() {
    local force_icons=false
    local app_args=()

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --install-icons)
                force_icons=true
                shift
                ;;
            *)
                app_args+=("$1")
                shift
                ;;
        esac
    done

    # Change to script directory
    cd "$SCRIPT_DIR"

    echo
    echo "${BOLD}$APP_NAME${RESET}"
    echo "────────────────────────────────────────"

    # Detect Linux distribution
    detect_distro
    log_info "Detected: $DISTRO_PRETTY_NAME ($DISTRO_ID)"

    # Run all checks
    check_python
    check_venv_support
    setup_venv
    check_and_install_packages

    # Install desktop integration only if explicitly requested
    # (automatic first-run installation is handled by Python)
    if [[ "$force_icons" == "true" ]]; then
        install_desktop_integration "true"
    fi

    echo "────────────────────────────────────────"

    # Launch the application
    launch_app "${app_args[@]}"
}

# Run main with all script arguments
main "$@"
