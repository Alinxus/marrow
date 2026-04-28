import SwiftUI

/// Marrow uses local auth — no sign-in required. This view auto-signs in.
struct SignInView: View {
  @ObservedObject var authState: AuthState

  var body: some View {
    ZStack {
      OmiColors.backgroundPrimary.ignoresSafeArea()

      VStack(spacing: 32) {
        Spacer()

        VStack(spacing: 16) {
          if let logoURL = Bundle.resourceBundle.url(forResource: "herologo", withExtension: "png"),
            let logoImage = NSImage(contentsOf: logoURL)
          {
            Image(nsImage: logoImage)
              .resizable()
              .aspectRatio(contentMode: .fit)
              .frame(width: 64, height: 64)
          }

          Text("Marrow")
            .scaledFont(size: 48, weight: .bold)
            .foregroundColor(OmiColors.textPrimary)

          Text("Ambient intelligence")
            .font(.title3)
            .foregroundColor(OmiColors.textTertiary)
        }

        Spacer()

        VStack(spacing: 12) {
          Button(action: {
            AuthService.shared.configure()
          }) {
            Text("Get Started")
              .scaledFont(size: 17, weight: .medium)
              .foregroundColor(.black)
              .frame(maxWidth: .infinity)
              .frame(height: 50)
              .background(Color.white)
              .cornerRadius(10)
          }
          .buttonStyle(.plain)
          .frame(width: 320)

          if let error = authState.error {
            Text(error)
              .font(.caption)
              .foregroundColor(OmiColors.error)
              .multilineTextAlignment(.center)
              .padding(.top, 4)
          }
        }

        Spacer().frame(height: 60)
      }
      .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
    .onAppear {
      // Auto-sign in on first launch
      if !authState.isSignedIn {
        AuthService.shared.configure()
      }
    }
  }
}

#Preview {
  SignInView(authState: AuthState.shared)
}
