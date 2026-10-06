pkgname=tracker-application
pkgver=0.11.0
pkgrel=2
pkgdesc="Local-first personal task, progress, activity and learning tracker"
arch=('any')
url="https://github.com/Verlopat/tracker-application"
license=('MIT')
depends=('python' 'pyside6' 'qt6-multimedia')

# This recipe is built from a complete checkout, including local fixes.
# Do not download main here: tagged/older checkouts must package their own code.
# Resolve this checkout once while makepkg sources the recipe. Stage only source
# files in srcdir so local edits and tagged checkouts package exactly what was tested.
_checkout_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

prepare() {
    rm -rf -- "$srcdir/tracker-application"
    install -d "$srcdir/tracker-application/app" "$srcdir/tracker-application/packaging"
    install -m644 "$_checkout_dir/main.py" "$_checkout_dir/LICENSE" "$srcdir/tracker-application/"
    install -m644 "$_checkout_dir"/app/*.py "$srcdir/tracker-application/app/"
    install -m644 "$_checkout_dir"/packaging/* "$srcdir/tracker-application/packaging/"
}

package() {
    cd "$srcdir/tracker-application"
    install -Dm755 packaging/tracker-application "$pkgdir/usr/bin/tracker-application"
    install -Dm644 packaging/tracker-application.desktop "$pkgdir/usr/share/applications/tracker-application.desktop"
    install -Dm644 packaging/tracker-application.svg "$pkgdir/usr/share/icons/hicolor/scalable/apps/tracker-application.svg"
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
    install -Dm644 main.py "$pkgdir/usr/share/tracker-application/main.py"
    install -d "$pkgdir/usr/share/tracker-application/app"
    install -m644 app/*.py "$pkgdir/usr/share/tracker-application/app/"
    python -m compileall -q -d /usr/share/tracker-application "$pkgdir/usr/share/tracker-application"
}
