Shader "AudioShader"
{
    Properties
    {
        _maxSPLDisplayed("Max spl (dB)", float)=150
        _frequency("Frequency (Hz)", Range(20.0,20000.0))=50
        [Toggle] _gradientDisplay("display gradient", float)=1
    }
    SubShader
    {
        Tags { "RenderType"="Opaque" }
        LOD 100

        Pass
        {
            CGPROGRAM
            #pragma vertex vert
            #pragma fragment frag            

            #include "UnityCG.cginc"
            uniform float _maxSPLDisplayed;
            
            uniform float _frequency;
            uniform float _soundSpeedInMetersPerSecond;

            uniform float _gradientDisplay;
            uniform float _gradientDiscreteChangeInDb;
            uniform fixed4 _gradientColors[10];

            uniform int _audioSourcesCount;

            uniform sampler2D _audioDataTexture;   
            
            uniform int _textureVerticalSize;
            

            struct appdata
            {
                float4 vertex : POSITION;
            };

            struct v2f
            {
                float4 vertex : SV_POSITION;
                float3 worldPos: TEXCOORD0;
            };
            
            v2f vert (appdata v)
            {
                v2f o;
                o.vertex = UnityObjectToClipPos(v.vertex);
                o.worldPos = mul (unity_ObjectToWorld, v.vertex);
                return o;
            }
            
            float distanceFromPlane(float3 pixel, float3 planeNormal, float planeDistanceFromOrigin){
                float d = dot(planeNormal,pixel);
                return d+planeDistanceFromOrigin;
            }

            float AttenuationBasedOnFrustum(float3 pixel, int audioSourceIndex){
                for (float i=7.5;i<27;i+=4){
                    float normalX = tex2D(_audioDataTexture, float2((audioSourceIndex+0.5)/_audioSourcesCount,i/_textureVerticalSize));
                    float normalY = tex2D(_audioDataTexture, float2((audioSourceIndex+0.5)/_audioSourcesCount,(i+1)/_textureVerticalSize));
                    float normalZ = tex2D(_audioDataTexture, float2((audioSourceIndex+0.5)/_audioSourcesCount,(i+2)/_textureVerticalSize));
                    float distanceFromOrigin = tex2D(_audioDataTexture, float2((audioSourceIndex+0.5)/_audioSourcesCount,(i+3)/_textureVerticalSize));
                    if (distanceFromPlane(pixel, float3(normalX,normalY,normalZ), distanceFromOrigin)>0){
                        return 0;
                    }
                }
                return 1;
            }

            fixed4 frag(v2f i): SV_Target{                
                float soundVectorXSum;
                float soundVectorYSum;
                for (int j=0; j<_audioSourcesCount; j++){      
                    float posX = tex2D(_audioDataTexture, float2((j+0.5)/_audioSourcesCount,0.5/_textureVerticalSize));    
                    float posY = tex2D(_audioDataTexture, float2((j+0.5)/_audioSourcesCount,1.5/_textureVerticalSize));
                    float posZ = tex2D(_audioDataTexture, float2((j+0.5)/_audioSourcesCount,2.5/_textureVerticalSize));
                    float dbSPLAtOneMeter = tex2D(_audioDataTexture, float2((j+0.5)/_audioSourcesCount,3.5/_textureVerticalSize));
                    float delay = tex2D(_audioDataTexture, float2((j+0.5)/_audioSourcesCount,4.5/_textureVerticalSize));
                    float polarity=tex2D(_audioDataTexture, float2((j+0.5)/_audioSourcesCount,5.5/_textureVerticalSize));
                    
                    float3 _audioSourcePosition = float3(posX,posY,posZ);
                    float dist = distance(i.worldPos,_audioSourcePosition);

                    float actual_dbSPL = dbSPLAtOneMeter-20*(log(dist)/log(10));
                    float actualPascal = pow(10,actual_dbSPL/20)*20*pow(10,-6);                    

                    //somma la pressione attuale a quella totale tenendo conto dello sfasamento in frequenza                    
                    float phase = 2 * 3.1415926535897932384626433832795 * (dist+delay) * _frequency / _soundSpeedInMetersPerSecond;

                    //calcola l'incidenza del diffusore audio, in base all'apertura in gradi
                    float forceAudioOmnidirectional = tex2D(_audioDataTexture, float2((j+0.5)/_audioSourcesCount,6.5/_textureVerticalSize));                   
                    float soundDispersionFactor;
                    if (forceAudioOmnidirectional==1.0f){
                        soundDispersionFactor=1.0f;
                    }
                    else{
                        soundDispersionFactor = AttenuationBasedOnFrustum(i.worldPos, j);
                    }

                    soundVectorXSum+=actualPascal*sin(phase)*polarity*soundDispersionFactor;
                    soundVectorYSum+=actualPascal*cos(phase)*polarity*soundDispersionFactor;

                }          
                float soundIntensity = sqrt(soundVectorXSum*soundVectorXSum+soundVectorYSum*soundVectorYSum);
                float total_dbSPL = 20*log(soundIntensity/(20*pow(10,-6)))/log(10);    

                //cambio di colore ogni 6 dB di differenza                    
                float value = (_maxSPLDisplayed-total_dbSPL)/_gradientDiscreteChangeInDb;
                if (value>=10){
                    value=9;
                }
                fixed4 col = _gradientColors[value];              
                if (_gradientDisplay){
                    float lerpFactor = value - (int)value;
                    col = lerp(col,_gradientColors[value+1],lerpFactor);
                }
                //colora il pixel                                
                return col;
            }           
            ENDCG
        }
    }
}
